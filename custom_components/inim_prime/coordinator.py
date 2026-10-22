"""DataUpdateCoordinator for the INIM Prime integration (native TCP 6004 only)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Protocol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    Area,
    AreaMode,
    AreaState,
    Fault,
    Local6004Client,
    Local6004Config,
    Local6004Error,
    Local6004Structure,
    NativeAreaStatus,
    NativeSystemStatus,
    NativeZoneStatus,
    Output,
    Scenario,
    Version,
    Zone,
    ZoneState,
    scene_is_active,
)
from .const import (
    DEFAULT_CYCLE_TIMEOUT,
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
    NATIVE_BACKOFF_INTERVAL,
    NATIVE_POLL_INTERVAL,
)


@dataclass
class InimData:
    """All panel state read in a single coordinator cycle."""

    version: Version
    areas: list[Area]
    zones: list[Zone]
    scenarios: list[Scenario]
    outputs: list[Output]
    fault: Fault


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
    """Coordinator that reads the panel's live state over the native protocol.

    Which areas, zones, scenarios and outputs exist, and their names, come from
    the native structure read once at setup (``local_config``). Every cycle then
    reads the live state with a handful of read-only status commands (~10 ms
    each): the partitions, the zone terminals, the output terminals and the
    system status (faults, tampers, supply voltage).
    """

    config_entry: InimConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: InimConfigEntry,
        client: Local6004Client,
        local_config: Local6004Config,
    ) -> None:
        """Initialize the coordinator."""
        self.client = client
        # The static config read at setup: firmware, scenario definitions,
        # zone->area map and the structure (objects and labels; setup refuses a
        # panel whose areas or zones could not be read).
        self.local_config = local_config
        self.structure: Local6004Structure = local_config.structure or Local6004Structure()
        # User preference (the "force arm on open zones" switch): when True the
        # apply-scenario buttons bypass open zones instead of failing.
        self.force_arm_on_open = False
        # A refresh that fires while a cycle is still reading coalesces onto the
        # running one instead of queueing a second.
        self._fetch_lock = asyncio.Lock()
        # Consecutive failed cycles: served from the last snapshot up to
        # FAILURES_BEFORE_UNAVAILABLE, polled less often from
        # FAILURES_BEFORE_BACKOFF so a panel that stopped answering is not
        # hammered.
        self._consecutive_failures = 0
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=NATIVE_POLL_INTERVAL),
        )

    async def _async_update_data(self) -> InimData:
        """Read the live panel state, tolerating a transient failure.

        A cycle runs under ``asyncio.timeout(DEFAULT_CYCLE_TIMEOUT)``. A failed
        cycle serves the last snapshot while fewer than
        ``FAILURES_BEFORE_UNAVAILABLE`` cycles in a row failed, so a single slow
        answer does not flap every entity to unavailable.
        """
        if self._fetch_lock.locked() and self.data is not None:
            return self.data
        async with self._fetch_lock:
            try:
                async with asyncio.timeout(DEFAULT_CYCLE_TIMEOUT):
                    data = await self._read_live()
            except Local6004Error as err:
                return self._handle_failure(str(err), err)
            except TimeoutError as err:
                return self._handle_failure(
                    f"panel did not respond within {DEFAULT_CYCLE_TIMEOUT}s", err
                )
            self._note_success()
            return data

    async def _read_live(self) -> InimData:
        """Read every live status the entities need, and build the snapshot."""
        zone_ids = {zone.id for zone in self.structure.zones or []}
        output_ids = {output.id for output in self.structure.outputs or []}
        areas = await self.client.async_get_area_statuses()
        zones = await self.client.async_get_zone_statuses(zone_ids) if zone_ids else {}
        outputs = await self.client.async_get_output_states(output_ids) if output_ids else {}
        system = await self.client.async_get_system_status()
        return self._build(areas, zones, outputs, system)

    def _build(
        self,
        area_statuses: dict[int, NativeAreaStatus],
        zone_statuses: dict[int, NativeZoneStatus],
        output_states: dict[int, int],
        system: NativeSystemStatus,
    ) -> InimData:
        """Build the snapshot from the structure and one set of live readings.

        An object the reading does not cover keeps its last known state (or a
        neutral one before the first reading: disarmed and ready, closed).
        """
        previous = self.data
        last_area = {area.id: area for area in previous.areas} if previous else {}
        last_zone = {zone.id: zone for zone in previous.zones} if previous else {}

        areas: list[Area] = []
        for obj in self.structure.areas or []:
            status = area_statuses.get(obj.id)
            if status is None:
                old = last_area.get(obj.id)
                areas.append(
                    replace(old, label=obj.label)
                    if old is not None
                    else Area(obj.id, obj.label, AreaMode.DISARMED, AreaState.READY, False)
                )
                continue
            state = (
                AreaState.ALARM
                if status.alarm
                else AreaState.SABOTAGE
                if status.sabotage
                else AreaState.READY
            )
            areas.append(Area(obj.id, obj.label, status.mode, state, status.alarm_memory))

        zones: list[Zone] = []
        for zdef in self.structure.zones or []:
            zstatus = zone_statuses.get(zdef.id)
            if zstatus is None:
                old_zone = last_zone.get(zdef.id)
                zones.append(
                    replace(old_zone, label=zdef.label)
                    if old_zone is not None
                    else Zone(zdef.id, zdef.label, zdef.terminal, ZoneState.READY, False, False)
                )
                continue
            zones.append(
                Zone(
                    zdef.id,
                    zdef.label,
                    zdef.terminal,
                    zstatus.state,
                    zstatus.alarm_memory,
                    zstatus.excluded,
                )
            )

        modes = {area.id: area.mode for area in areas}
        active = {
            scene.id for scene in self.local_config.scenes if scene_is_active(scene.arms, modes)
        }
        scenarios = [
            Scenario(id=obj.id, label=obj.label, active=obj.id in active)
            for obj in self.structure.scenarios or []
        ]
        outputs = [
            Output(id=obj.id, label=obj.label, state=output_states.get(obj.id))
            for obj in self.structure.outputs or []
        ]
        fault = Fault(
            vcc=system.supply_voltage,
            has_faults=system.has_faults,
            flags=dict(system.faults),
            raw=system.raw.hex(),
        )
        version = Version(raw=self.local_config.firmware, service=system.service)
        return InimData(
            version=version,
            areas=areas,
            zones=zones,
            scenarios=scenarios,
            outputs=outputs,
            fault=fault,
        )

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

    def _note_failure(self) -> None:
        """Record a failed cycle and slow down after the threshold."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= FAILURES_BEFORE_BACKOFF:
            self.update_interval = timedelta(seconds=NATIVE_BACKOFF_INTERVAL)

    def _note_success(self) -> None:
        """Clear the failure state and resume the normal cadence."""
        self._consecutive_failures = 0
        self.update_interval = timedelta(seconds=NATIVE_POLL_INTERVAL)

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
            patched_fault = replace(data.fault, has_faults=False)
            return replace(data, fault=patched_fault)

        return None

    # ------------------------------------------------------------------
    # Multi-active scenes (from the static scenario definitions)
    # ------------------------------------------------------------------
    def active_scene_ids(self) -> set[int]:
        """Return the ids of every scenario currently "active".

        A scenario is active when every partition it targets matches that
        target mode in the live area state.
        """
        if self.data is None:
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

    client: Local6004Client
    coordinator: InimDataUpdateCoordinator


type InimConfigEntry = ConfigEntry[InimRuntimeData]
