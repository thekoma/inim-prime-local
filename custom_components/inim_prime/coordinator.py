"""DataUpdateCoordinator for the INIM Prime integration."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Protocol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    ApiStats,
    ApiStatus,
    Area,
    AreaMode,
    AreaState,
    Fault,
    InimApiError,
    InimConnectionError,
    InimPrimeClient,
    Local6004Client,
    Local6004Config,
    Local6004Error,
    Local6004Structure,
    NativeAreaStatus,
    NativeObject,
    NativeZoneDef,
    NativeZoneStatus,
    Output,
    Scenario,
    Version,
    Zone,
    ZoneState,
    scene_is_active,
)
from .const import (
    API_STATS_REFRESH_INTERVAL,
    CONF_NATIVE_AREA_POLL,
    CONF_SCAN_INTERVAL_ACTIVE,
    CONF_SCAN_INTERVAL_IDLE,
    DEFAULT_ACTIVE_WINDOW,
    DEFAULT_CYCLE_TIMEOUT,
    DEFAULT_NATIVE_AREA_POLL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_ACTIVE,
    DEFAULT_SCAN_INTERVAL_IDLE,
    DOMAIN,
    EV_ALARM,
    EV_ARM,
    EV_DISARM,
    EV_FAULT,
    EV_FAULT_RESTORE,
    EV_OUTPUT,
    EV_TAMPER,
    EV_ZONE_CLOSE,
    EV_ZONE_OPEN,
    FAILURES_BEFORE_BACKOFF,
    FAILURES_BEFORE_UNAVAILABLE,
    LOGGER,
    NATIVE_AREA_BACKOFF_TICKS,
    NATIVE_AREA_FAILURES_BEFORE_BACKOFF,
    NATIVE_CGI_INTERVAL,
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


def _replace_in_list[IdT: _HasId](items: list[IdT], item: IdT) -> list[IdT]:
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
        # Static config read once over the optional read-only 6004 channel
        # (multi-active scene definitions + zone->area). None when 6004 is
        # disabled, unreachable, or the firmware layout is unsupported.
        self.local_config: Local6004Config | None = None
        # User preference (the "force arm on open zones" switch): when True the
        # apply-scenario buttons bypass open zones instead of failing.
        self.force_arm_on_open = False

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
            seconds=entry.options.get(CONF_SCAN_INTERVAL_ACTIVE, DEFAULT_SCAN_INTERVAL_ACTIVE)
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

        # The diagnostic api-stats read is refreshed at most every
        # API_STATS_REFRESH_INTERVAL seconds; between refreshes the cached value
        # is reused so each cycle costs one cgi read less.
        self._api_stats: ApiStats | None = None
        self._api_stats_at: float | None = None

        # Fast area-state path over the native 6004 status command. Set by
        # setup when the option is enabled; failures never mark entities
        # unavailable (the cgi poll stays authoritative for availability).
        self.native_client: Local6004Client | None = None
        # Set by setup when the "native commands" option is on: write commands
        # then go over 6004 first (see commands.py). Independent of the poll.
        self.command_client: Local6004Client | None = None
        self._native_failures = 0
        self._native_skip = 0
        # Last successful native reading and when it started (monotonic), so
        # a cgi cycle that began before it cannot roll the state back.
        self._native_areas: dict[int, NativeAreaStatus] = {}
        self._native_zones: dict[int, NativeZoneStatus] = {}
        self._native_at: float | None = None
        # Set while a native poll runs: a tick that fires meanwhile is skipped
        # rather than queued behind the status lock.
        self._native_polling = False
        # Object kinds whose cgi and native sets differed (warned once each).
        self._structure_warned: set[str] = set()

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

        A failed cycle does not immediately mark entities unavailable: while we
        have a cached snapshot, up to ``FAILURES_BEFORE_UNAVAILABLE - 1``
        consecutive failures are absorbed by serving that snapshot. A loaded
        panel occasionally misses a cycle, and flapping every entity to
        ``unavailable`` for that makes state-change automations miss events.

        After ``FAILURES_BEFORE_BACKOFF`` consecutive failed cycles we relax to
        the idle tier and stop fast polling. The counter resets on success.
        """
        # Coalesce: if a cycle is already running, don't pile a second one on
        # top. Return the last good snapshot when we have one.
        if self._fetch_lock.locked() and self.data is not None:
            return self.data

        async with self._fetch_lock:
            started = time.monotonic()
            try:
                async with asyncio.timeout(DEFAULT_CYCLE_TIMEOUT):
                    data = await self._fetch_cycle()
            except InimApiError as err:
                # An API-key rejection is not a transient failure: surface it as
                # ConfigEntryAuthFailed so Home Assistant starts the reauth flow
                # to let the user re-enter the key.
                if err.status == ApiStatus.ERROR_APIKEY:
                    self._note_failure()
                    raise ConfigEntryAuthFailed(str(err)) from err
                return self._handle_failure(str(err), err)
            except InimConnectionError as err:
                return self._handle_failure(str(err), err)
            except TimeoutError as err:
                return self._handle_failure(
                    f"panel did not respond within {DEFAULT_CYCLE_TIMEOUT}s", err
                )

            self._note_success()
            return self._overlay_native(data, started)

    def _overlay_native(self, data: InimData, started: float) -> InimData:
        """Re-apply a native reading taken after this cgi cycle started.

        A cgi cycle takes seconds; if the native poll saw a change meanwhile,
        the cgi snapshot is older and must not roll that change back.
        """
        if self._native_at is None or self._native_at < started:
            return data
        areas = self._patch_areas(data, self._native_areas) or data
        return self.apply_native_zones(areas, self._native_zones) or areas

    def _handle_failure(self, message: str, err: Exception) -> InimData:
        """Absorb a transient failed cycle, or raise once it is persistent.

        Returns the last good snapshot while the consecutive-failure count is
        below ``FAILURES_BEFORE_UNAVAILABLE``; otherwise (or with no snapshot
        yet) raises ``UpdateFailed`` so entities go unavailable.
        """
        self._note_failure()
        if self.data is not None and self._consecutive_failures < FAILURES_BEFORE_UNAVAILABLE:
            LOGGER.debug(
                "Update cycle failed (%d/%d), keeping last snapshot: %s",
                self._consecutive_failures,
                FAILURES_BEFORE_UNAVAILABLE,
                message,
            )
            return self.data
        raise UpdateFailed(message) from err

    async def _fetch_cycle(self) -> InimData:
        """Issue the sequential cgi reads that make up one update cycle.

        The cgi endpoint is single-threaded, so reads are issued sequentially.
        The optional ``api_stats`` read degrades to ``None`` instead of failing
        the whole update, and is only re-read every
        ``API_STATS_REFRESH_INTERVAL`` seconds.
        """
        if self._version is None:
            self._version = await self.client.version()

        areas = await self.client.get_areas()
        zones = await self.client.get_zones()
        scenarios = await self.client.get_scenarios()
        outputs = await self.client.get_outputs()
        fault = await self.client.get_faults()

        now = time.monotonic()
        if self._api_stats_at is None or now - self._api_stats_at >= API_STATS_REFRESH_INTERVAL:
            try:
                self._api_stats = await self.client.get_api_stats()
                self._api_stats_at = now
            except (InimConnectionError, InimApiError):
                # Retry on the next cycle rather than waiting a full interval.
                self._api_stats = None
        api_stats = self._api_stats

        return self._apply_structure(
            InimData(
                version=self._version,
                areas=areas,
                zones=zones,
                scenarios=scenarios,
                outputs=outputs,
                fault=fault,
                api_stats=api_stats,
            )
        )

    # ------------------------------------------------------------------
    # Native structure: which objects exist, and their labels
    # ------------------------------------------------------------------
    def _apply_structure(self, data: InimData) -> InimData:
        """Overlay the native structure (read once at setup) on a cgi snapshot.

        The cgi cycle still reads everything; this step then applies the
        native labels and object sets, in the native order:

        * an object both report keeps its cgi state, with the native label;
        * an area, zone or scenario only the cgi reports is kept as the cgi
          has it (the union): a security object is never hidden on the
          strength of the native existence rules alone;
        * an area or zone only the native structure lists is added only while
          the native poll is enabled, since that poll is what keeps its state
          live. It starts from the last native status reading, or a neutral
          state (disarmed/ready, closed) until the first tick;
        * a scenario only the native structure lists is added as inactive;
        * outputs come from the native structure only, which fixes the cgi's
          output list (two outputs, named after zones). An output the cgi does
          not report has an unknown state.

        A kind the native read could not provide (None), or no native
        structure at all, keeps the cgi's list unchanged.
        """
        structure = self.local_config.structure if self.local_config is not None else None
        if structure is None:
            return data
        self._warn_structure_mismatch(data, structure)
        native_poll = bool(
            self.config_entry.options.get(CONF_NATIVE_AREA_POLL, DEFAULT_NATIVE_AREA_POLL)
        )
        return replace(
            data,
            areas=self._merge_areas(data.areas, structure.areas, native_poll),
            zones=self._merge_zones(data.zones, structure.zones, native_poll),
            scenarios=self._merge_scenarios(data.scenarios, structure.scenarios),
            outputs=self._merge_outputs(data.outputs, structure.outputs),
        )

    def _merge_areas(
        self, cgi: list[Area], native: list[NativeObject] | None, native_poll: bool
    ) -> list[Area]:
        if native is None:
            return cgi
        by_id = {area.id: area for area in cgi}
        areas: list[Area] = []
        for obj in native:
            area = by_id.pop(obj.id, None)
            if area is not None:
                areas.append(replace(area, label=obj.label))
            elif native_poll:
                status = self._native_areas.get(obj.id)
                areas.append(
                    Area(
                        id=obj.id,
                        label=obj.label,
                        mode=status.mode if status is not None else AreaMode.DISARMED,
                        state=(
                            AreaState.ALARM
                            if status is not None and status.alarm
                            else AreaState.READY
                        ),
                        alarm_memory=status is not None and status.alarm_memory,
                    )
                )
        return areas + list(by_id.values())

    def _merge_zones(
        self, cgi: list[Zone], native: list[NativeZoneDef] | None, native_poll: bool
    ) -> list[Zone]:
        if native is None:
            return cgi
        by_id = {zone.id: zone for zone in cgi}
        zones: list[Zone] = []
        for zdef in native:
            zone = by_id.pop(zdef.id, None)
            if zone is not None:
                zones.append(replace(zone, label=zdef.label))
            elif native_poll:
                status = self._native_zones.get(zdef.id)
                zones.append(
                    Zone(
                        id=zdef.id,
                        label=zdef.label,
                        # The cgi reports a zone's own id as its terminal.
                        terminal=zdef.id,
                        state=status.state if status is not None else ZoneState.READY,
                        alarm_memory=status is not None and status.alarm_memory,
                        excluded=status is not None and status.excluded,
                    )
                )
        return zones + list(by_id.values())

    @staticmethod
    def _merge_scenarios(
        cgi: list[Scenario], native: list[NativeObject] | None
    ) -> list[Scenario]:
        if native is None:
            return cgi
        by_id = {scenario.id: scenario for scenario in cgi}
        scenarios = [
            replace(by_id.pop(obj.id), label=obj.label)
            if obj.id in by_id
            else Scenario(id=obj.id, label=obj.label, active=False)
            for obj in native
        ]
        return scenarios + list(by_id.values())

    @staticmethod
    def _merge_outputs(cgi: list[Output], native: list[NativeObject] | None) -> list[Output]:
        if native is None:
            return cgi
        by_id = {output.id: output for output in cgi}
        return [
            replace(by_id[obj.id], label=obj.label)
            if obj.id in by_id
            else Output(id=obj.id, label=obj.label, terminal=obj.id, state=None, type=0)
            for obj in native
        ]

    def _warn_structure_mismatch(self, data: InimData, structure: Local6004Structure) -> None:
        """Log once per kind when the cgi and native object sets differ.

        They match on the panels verified so far; a difference means the native
        existence rules miss a case and is worth a bug report. Outputs are not
        compared: the cgi output list is known to be wrong.
        """

        def ids(items: list[NativeObject] | list[NativeZoneDef] | None) -> set[int] | None:
            return None if items is None else {item.id for item in items}

        for kind, cgi_ids, native_ids in (
            ("areas", {a.id for a in data.areas}, ids(structure.areas)),
            ("zones", {z.id for z in data.zones}, ids(structure.zones)),
            ("scenarios", {s.id for s in data.scenarios}, ids(structure.scenarios)),
        ):
            if native_ids is None or cgi_ids == native_ids or kind in self._structure_warned:
                continue
            self._structure_warned.add(kind)
            LOGGER.warning(
                "Native panel structure differs from the cgi for %s: only cgi %s, only native %s."
                " Keeping both; please report this",
                kind,
                sorted(cgi_ids - native_ids),
                sorted(native_ids - cgi_ids),
            )

    @callback
    def _note_failure(self) -> None:
        """Record a failed cycle and back off after the threshold."""
        self._consecutive_failures += 1
        if not self._backed_off and self._consecutive_failures >= FAILURES_BEFORE_BACKOFF:
            self._backed_off = True
            # Stop hammering: cancel any pending fast-poll decay and pin idle.
            self.async_cancel_decay()
            if self.update_interval != self.rest_interval:
                self.update_interval = self.rest_interval
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

        self._cancel_decay = async_call_later(self.hass, self._active_window, self._decay_to_idle)

    @callback
    def _decay_to_idle(self, _now: object = None) -> None:
        """Relax back to the resting interval after the active window."""
        self._cancel_decay = None
        self._relax_to_rest()

    @property
    def native_healthy(self) -> bool:
        """Return True while the native poll is attached and not backing off."""
        return (
            self.native_client is not None
            and self._native_failures < NATIVE_AREA_FAILURES_BEFORE_BACKOFF
        )

    @property
    def rest_interval(self) -> timedelta:
        """Resting cgi interval: slow while the native poll covers live state.

        Areas, zones and scenario state come from the native poll, so the cgi
        is only needed for outputs, faults and diagnostics. When the native
        channel fails, fall back to the configured idle interval.
        """
        if self.native_healthy:
            return max(self._idle_interval, timedelta(seconds=NATIVE_CGI_INTERVAL))
        return self._idle_interval

    @callback
    def _relax_to_rest(self) -> None:
        """Apply the resting interval unless a fast-poll window is running."""
        if self._cancel_decay is not None:
            return
        if self.update_interval != self.rest_interval:
            self.update_interval = self.rest_interval
            self._schedule_refresh()

    @callback
    def async_attach_native(self, client: Local6004Client) -> None:
        """Attach the native client and relax the cgi to its resting interval."""
        self.native_client = client
        self._relax_to_rest()

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
    # Native fast path: live area + zone state over the 6004 status commands
    # ------------------------------------------------------------------
    async def async_native_poll(self, _now: object = None) -> None:
        """Read live area and zone state natively and push any change to entities.

        On a change the cached snapshot is patched and published immediately.
        An area change also arms a fast cgi poll to reconcile the rest of the
        state; zone changes do not (zones are fully covered natively, and doors
        opening must not keep the cgi in its fast tier). After repeated
        failures the poll backs off for NATIVE_AREA_BACKOFF_TICKS ticks.
        """
        if self.native_client is None or self.data is None:
            return
        # The interval timer starts a new task every tick even while the last
        # read is still waiting on a slow panel; skip instead of piling up.
        if self._native_polling:
            return
        if self._native_skip > 0:
            self._native_skip -= 1
            return
        self._native_polling = True
        try:
            await self._native_poll(self.native_client)
        finally:
            self._native_polling = False

    async def _native_poll(self, native_client: Local6004Client) -> None:
        """Run one native read and apply it (see :meth:`async_native_poll`)."""
        zone_ids = {zone.id for zone in self.data.zones}
        read_at = time.monotonic()
        try:
            statuses = await native_client.async_get_area_statuses()
            zone_statuses = (
                await native_client.async_get_zone_statuses(zone_ids) if zone_ids else {}
            )
        except Local6004Error as err:
            self._native_failures += 1
            LOGGER.debug("Native poll failed (%d): %s", self._native_failures, err)
            if self._native_failures >= NATIVE_AREA_FAILURES_BEFORE_BACKOFF:
                self._native_skip = NATIVE_AREA_BACKOFF_TICKS
                # The cgi is authoritative again: back to the idle interval.
                self._relax_to_rest()
            return
        recovered = self._native_failures >= NATIVE_AREA_FAILURES_BEFORE_BACKOFF
        self._native_failures = 0
        if recovered:
            self._relax_to_rest()
        self._native_areas, self._native_zones, self._native_at = (
            statuses,
            zone_statuses,
            read_at,
        )

        area_patch = self.apply_native_statuses(statuses)
        zone_patch = self.apply_native_zones(area_patch or self.data, zone_statuses)
        patched = zone_patch or area_patch
        if patched is not None:
            # Publish without async_set_updated_data(): that would reschedule
            # the cgi refresh on every change, and frequent zone activity
            # would then starve the cgi poll.
            self.data = patched
            self.async_update_listeners()
        if area_patch is not None:
            self.activate_fast_poll()

    @staticmethod
    def apply_native_zones(
        data: InimData, statuses: dict[int, NativeZoneStatus]
    ) -> InimData | None:
        """Return ``data`` with native zone state applied, or None if unchanged."""
        zones: list[Zone] = []
        changed = False
        for zone in data.zones:
            native = statuses.get(zone.id)
            if native is None:
                zones.append(zone)
                continue
            new = replace(
                zone,
                state=native.state,
                alarm_memory=native.alarm_memory,
                excluded=native.excluded,
            )
            changed = changed or new != zone
            zones.append(new)
        return replace(data, zones=zones) if changed else None

    def apply_native_statuses(self, statuses: dict[int, NativeAreaStatus]) -> InimData | None:
        """Return a snapshot with native area state applied, or None if unchanged."""
        if self.data is None:
            return None
        return self._patch_areas(self.data, statuses)

    @staticmethod
    def _patch_areas(data: InimData, statuses: dict[int, NativeAreaStatus]) -> InimData | None:
        """Return ``data`` with native area state applied, or None if unchanged."""
        areas: list[Area] = []
        changed = False
        for area in data.areas:
            native = statuses.get(area.id)
            if native is None:
                areas.append(area)
                continue
            state = area.state
            if native.alarm:
                state = AreaState.ALARM
            elif state is AreaState.ALARM:
                state = AreaState.READY
            new = replace(area, mode=native.mode, state=state, alarm_memory=native.alarm_memory)
            changed = changed or new != area
            areas.append(new)
        return replace(data, areas=areas) if changed else None

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
            new_state = ZoneState.ALARM if ev == EV_ZONE_OPEN else ZoneState.READY
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
            patched_area = replace(area, mode=new_mode, state=AreaState.READY, alarm_memory=False)
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
            return replace(data, outputs=_replace_in_list(data.outputs, patched_output))

        if ev in (EV_FAULT, EV_TAMPER):
            patched_fault = replace(data.fault, has_faults=True)
            return replace(data, fault=patched_fault)

        if ev == EV_FAULT_RESTORE:
            # We cannot recompute the precise fault bitmap from a webhook; mark
            # cleared optimistically and let the next poll reconcile the detail.
            patched_fault = replace(data.fault, has_faults=False, raw_fau="0")
            return replace(data, fault=patched_fault)

        return None

    # ------------------------------------------------------------------
    # Multi-active scenes (from the optional read-only 6004 channel)
    # ------------------------------------------------------------------
    def active_scene_ids(self) -> set[int]:
        """Return the ids of every scenario currently "active".

        A scenario is active when every partition it targets matches that
        target mode in the live cgi area state. Empty when 6004 is unavailable.
        """
        if self.local_config is None or self.data is None:
            return set()
        modes_by_area = {area.id: area.mode for area in self.data.areas}
        return {
            scene.id
            for scene in self.local_config.scenes
            if scene_is_active(scene.arms, modes_by_area)
        }

    @staticmethod
    def _find[IdT: _HasId](items: list[IdT], raw_id: str | None) -> IdT | None:
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
    local_client: Local6004Client | None = None


type InimConfigEntry = ConfigEntry[InimRuntimeData]
