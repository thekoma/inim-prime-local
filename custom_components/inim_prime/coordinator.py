"""DataUpdateCoordinator for the INIM Prime integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Protocol, TypeVar

from .client import (
    ApiStatus,
    InimApiError,
    InimConnectionError,
    InimPrimeClient,
    ApiStats,
    Area,
    AreaMode,
    AreaState,
    Fault,
    Output,
    Scenario,
    Version,
    Zone,
    ZoneState,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_SCAN_INTERVAL_ACTIVE,
    CONF_SCAN_INTERVAL_IDLE,
    DEFAULT_ACTIVE_WINDOW,
    DEFAULT_CYCLE_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_ACTIVE,
    DEFAULT_SCAN_INTERVAL_IDLE,
    DOMAIN,
    FAILURES_BEFORE_BACKOFF,
    EV_ALARM,
    EV_ARM,
    EV_DISARM,
    EV_FAULT,
    EV_FAULT_RESTORE,
    EV_OUTPUT,
    EV_TAMPER,
    EV_ZONE_CLOSE,
    EV_ZONE_OPEN,
    LOGGER,
)


@dataclass
class InimData:
    """All panel state fetched in a single coordinator cycle."""

    version: Version
    areas: list[Area]
    zones: list[Zone]
    scenarios: list[Scenario]
    outputs: list[Output]
    fault: Fault
    api_stats: ApiStats | None


class _HasId(Protocol):
    """Anything with an integer ``id`` (Zone/Area/Output/Scenario/...)."""

    @property
    def id(self) -> int: ...


_IdT = TypeVar("_IdT", bound=_HasId)


def _replace_in_list(items: list[_IdT], item: _IdT) -> list[_IdT]:
    """Return a new list with the element whose ``.id`` matches replaced.

    If no element matches, the original list is returned unchanged.
    """
    return [item if existing.id == item.id else existing for existing in items]


class InimDataUpdateCoordinator(DataUpdateCoordinator[InimData]):
    """Coordinator that polls the INIM PrimeX panel sequentially.

    Supports adaptive two-tier polling: a slow *idle* interval that relaxes
    cost when nothing is happening, and a fast *active* interval entered for a
    short window after a webhook event (or a detected change), then decayed
    back to idle. The full poll always runs as a reconciliation backstop.
    """

    config_entry: InimConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: InimConfigEntry,
        client: InimPrimeClient,
    ) -> None:
        """Initialize the coordinator."""
        self.client = client
        self._version: Version | None = None

        # Adaptive interval configuration. The legacy ``scan_interval`` option
        # still acts as the idle baseline when the new options are absent, so
        # existing setups keep their behavior.
        legacy_scan = entry.options.get(
            "scan_interval",
            entry.data.get("scan_interval", DEFAULT_SCAN_INTERVAL),
        )
        self._idle_interval = timedelta(
            seconds=entry.options.get(
                CONF_SCAN_INTERVAL_IDLE,
                legacy_scan if legacy_scan is not None else DEFAULT_SCAN_INTERVAL_IDLE,
            )
        )
        self._active_interval = timedelta(
            seconds=entry.options.get(
                CONF_SCAN_INTERVAL_ACTIVE, DEFAULT_SCAN_INTERVAL_ACTIVE
            )
        )
        self._active_window = DEFAULT_ACTIVE_WINDOW
        self._cancel_decay: Callable[[], None] | None = None

        # Re-entrancy guard: a refresh that fires while a fetch is still in
        # flight must coalesce onto the running one, never start/queue a second
        # concurrent cgi cycle (the cgi is effectively single-threaded).
        self._fetch_lock = asyncio.Lock()

        # Failure backoff: after FAILURES_BEFORE_BACKOFF consecutive failed
        # cycles we suspend fast polling and pin the idle tier so we stop
        # hammering a dead/slow panel. ``_backed_off`` gates re-arming fast poll.
        self._consecutive_failures = 0
        self._backed_off = False

        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=self._idle_interval,
        )

    async def _async_update_data(self) -> InimData:
        """Fetch the full panel state safely.

        Two guards protect a dead/slow panel from harming Home Assistant:

        * **No overlap** — a ``self._fetch_lock`` ensures at most one cgi cycle
          runs at a time. If a refresh fires while one is in flight (and we
          already have data), we coalesce by returning the cached snapshot
          instead of starting/queuing a second concurrent cycle.
        * **Hard ceiling** — the whole sequential fetch runs under
          ``asyncio.timeout(DEFAULT_CYCLE_TIMEOUT)`` so a stuck cycle can never
          run away; on timeout we raise ``UpdateFailed`` (entities go
          unavailable, coordinator backs off) rather than hang.

        After ``FAILURES_BEFORE_BACKOFF`` consecutive failed cycles we relax to
        the idle tier and stop fast polling. The counter resets on success.
        """
        # Coalesce: if a cycle is already running, don't pile a second one on
        # top. Return the last good snapshot when we have one.
        if self._fetch_lock.locked() and self.data is not None:
            return self.data

        async with self._fetch_lock:
            try:
                async with asyncio.timeout(DEFAULT_CYCLE_TIMEOUT):
                    data = await self._fetch_cycle()
            except InimApiError as err:
                self._note_failure()
                # An API-key rejection is not a transient failure: surface it as
                # ConfigEntryAuthFailed so Home Assistant starts the reauth flow
                # to let the user re-enter the key.
                if err.status == ApiStatus.ERROR_APIKEY:
                    raise ConfigEntryAuthFailed(str(err)) from err
                raise UpdateFailed(str(err)) from err
            except InimConnectionError as err:
                self._note_failure()
                raise UpdateFailed(str(err)) from err
            except TimeoutError as err:
                self._note_failure()
                raise UpdateFailed(
                    f"panel did not respond within {DEFAULT_CYCLE_TIMEOUT}s"
                ) from err

            self._note_success()
            return data

    async def _fetch_cycle(self) -> InimData:
        """Issue the sequential cgi reads that make up one update cycle.

        The cgi endpoint is single-threaded, so reads are issued sequentially.
        The optional ``api_stats`` read degrades to ``None`` instead of failing
        the whole update.
        """
        if self._version is None:
            self._version = await self.client.version()

        areas = await self.client.get_areas()
        zones = await self.client.get_zones()
        scenarios = await self.client.get_scenarios()
        outputs = await self.client.get_outputs()
        fault = await self.client.get_faults()

        api_stats: ApiStats | None
        try:
            api_stats = await self.client.get_api_stats()
        except (InimConnectionError, InimApiError):
            api_stats = None

        return InimData(
            version=self._version,
            areas=areas,
            zones=zones,
            scenarios=scenarios,
            outputs=outputs,
            fault=fault,
            api_stats=api_stats,
        )

    @callback
    def _note_failure(self) -> None:
        """Record a failed cycle and back off after the threshold."""
        self._consecutive_failures += 1
        if (
            not self._backed_off
            and self._consecutive_failures >= FAILURES_BEFORE_BACKOFF
        ):
            self._backed_off = True
            # Stop hammering: cancel any pending fast-poll decay and pin idle.
            self.async_cancel_decay()
            if self.update_interval != self._idle_interval:
                self.update_interval = self._idle_interval
                self._schedule_refresh()

    @callback
    def _note_success(self) -> None:
        """Clear the failure state after a successful cycle."""
        self._consecutive_failures = 0
        self._backed_off = False

    # ------------------------------------------------------------------
    # Adaptive polling
    # ------------------------------------------------------------------
    @callback
    def activate_fast_poll(self) -> None:
        """Switch to the fast (active) interval for the active window.

        Called after a webhook event or a detected change. The interval decays
        back to idle once the window elapses with no further activity.

        While the coordinator is in the failure-backoff state we refuse to
        re-arm fast polling: a dead/slow panel must not be hammered at the
        active cadence just because a stale/duplicate event arrived. A real
        recovery (a successful cycle) clears the backoff and re-enables this.
        """
        if self._backed_off:
            return

        if self.update_interval != self._active_interval:
            self.update_interval = self._active_interval
            # Reschedule the next refresh at the new (faster) cadence.
            self._schedule_refresh()

        if self._cancel_decay is not None:
            self._cancel_decay()

        self._cancel_decay = async_call_later(
            self.hass, self._active_window, self._decay_to_idle
        )

    @callback
    def _decay_to_idle(self, _now: object = None) -> None:
        """Relax back to the idle interval after the active window."""
        self._cancel_decay = None
        if self.update_interval != self._idle_interval:
            self.update_interval = self._idle_interval
            self._schedule_refresh()

    @callback
    def async_cancel_decay(self) -> None:
        """Cancel any pending decay timer.

        Must be called on unload/reload so a webhook that fired just before
        teardown cannot fire ``_decay_to_idle`` afterwards and re-arm polling
        on an unloaded coordinator.
        """
        if self._cancel_decay is not None:
            self._cancel_decay()
            self._cancel_decay = None

    async def async_shutdown(self) -> None:
        """Cancel the decay timer, then perform the base coordinator shutdown."""
        self.async_cancel_decay()
        await super().async_shutdown()

    # ------------------------------------------------------------------
    # Optimistic event patching (webhook fast-path)
    # ------------------------------------------------------------------
    def apply_event(self, ev: str, **params: str) -> InimData | None:
        """Return a shallow-patched ``InimData`` for a single panel event.

        The current cached snapshot is copied with the single affected
        zone/area/output/fault replaced (frozen dataclasses are rebuilt via
        :func:`dataclasses.replace`, so patches are immutable and idempotent).
        Returns ``None`` when there is no cached data yet, the event/params are
        unusable, or the target object is unknown — the caller then leaves
        reconciliation to the poll.
        """
        data = self.data
        if data is None:
            return None

        if ev in (EV_ZONE_OPEN, EV_ZONE_CLOSE):
            zone = self._find(data.zones, params.get("id"))
            if zone is None:
                return None
            new_state = (
                ZoneState.ALARM if ev == EV_ZONE_OPEN else ZoneState.READY
            )
            patched_zone = replace(zone, state=new_state)
            return replace(data, zones=_replace_in_list(data.zones, patched_zone))

        if ev in (EV_ARM, EV_DISARM):
            area = self._find(data.areas, params.get("area"))
            if area is None:
                return None
            new_mode = AreaMode.DISARMED if ev == EV_DISARM else AreaMode.TOTAL
            # Reset state to a non-alarm value and clear any latched alarm
            # memory: otherwise a prior 'alarm' event leaves state=ALARM /
            # alarm_memory=True, which the alarm panel still renders as
            # TRIGGERED regardless of mode, so the optimistic arm/disarm would
            # not visibly take effect until the next poll reconciles.
            patched_area = replace(
                area, mode=new_mode, state=AreaState.READY, alarm_memory=False
            )
            return replace(data, areas=_replace_in_list(data.areas, patched_area))

        if ev == EV_ALARM:
            area = self._find(data.areas, params.get("area"))
            if area is None:
                return None
            patched_area = replace(area, alarm_memory=True, state=AreaState.ALARM)
            return replace(data, areas=_replace_in_list(data.areas, patched_area))

        if ev == EV_OUTPUT:
            output = self._find(data.outputs, params.get("id"))
            if output is None:
                return None
            try:
                value = int(params.get("state", "1"))
            except (TypeError, ValueError):
                value = 1
            patched_output = replace(output, state=value)
            return replace(
                data, outputs=_replace_in_list(data.outputs, patched_output)
            )

        if ev in (EV_FAULT, EV_TAMPER):
            patched_fault = replace(data.fault, has_faults=True)
            return replace(data, fault=patched_fault)

        if ev == EV_FAULT_RESTORE:
            # We cannot recompute the precise fault bitmap from a webhook; mark
            # cleared optimistically and let the next poll reconcile the detail.
            patched_fault = replace(data.fault, has_faults=False, raw_fau="0")
            return replace(data, fault=patched_fault)

        return None

    @staticmethod
    def _find(items: list[_IdT], raw_id: str | None) -> _IdT | None:
        """Return the item whose ``.id`` matches ``raw_id`` (as int), or None."""
        if raw_id is None:
            return None
        try:
            target = int(raw_id)
        except (TypeError, ValueError):
            return None
        return next((i for i in items if i.id == target), None)


@dataclass
class InimRuntimeData:
    """Runtime data stored on the config entry."""

    client: InimPrimeClient
    coordinator: InimDataUpdateCoordinator


type InimConfigEntry = ConfigEntry[InimRuntimeData]
