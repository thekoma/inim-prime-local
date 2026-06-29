"""Binary sensor platform for the INIM Prime integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import FAULT_FLAG_KEYS, Area, Scenario, Zone, ZoneState
from .const import is_factory_default_area
from .coordinator import InimConfigEntry, InimDataUpdateCoordinator
from .device import (
    group_zones_by_room,
    label_language,
    panel_device_info,
    room_device_info,
)
from .room_guess import guess_room
from .zone_guess import guess_device_class

# Read-only platform: all state comes from the coordinator, no panel writes,
# so updates need not be serialized.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InimConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the INIM Prime binary sensors.

    Per-object sensors (zones, per-area alarm-memory, per-scenario) are added
    dynamically via a coordinator listener so objects appearing after setup show
    up without a reload. The fixed sensors (the "System fault" summary and the
    per-flag fault sensors) are added once on the first sync. Removed objects are
    not deleted from the registry; their entity reports ``available = False``.
    """
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def _sync() -> None:
        """Add binary sensors for object ids not yet known."""
        new_entities: list[BinarySensorEntity] = []

        def _add(entity: BinarySensorEntity) -> None:
            uid = entity.unique_id
            if uid is None or uid in known:
                return
            known.add(uid)
            new_entities.append(entity)

        # Fixed (panel-wide) sensors: created once, on the first sync.
        _add(InimFaultBinarySensor(coordinator))
        for key in FAULT_FLAG_KEYS:
            _add(InimFaultFlagBinarySensor(coordinator, key))

        for zone in coordinator.data.zones:
            _add(InimZoneBinarySensor(coordinator, zone.id))
        for area in coordinator.data.areas:
            _add(InimAreaAlarmMemoryBinarySensor(coordinator, area.id))
        # Multi-active scene sensors, enabled by default. Computed from the 6004
        # scenario definitions vs live area state, so several scenes can read
        # "on" at once — the accurate view the panel's single ``st`` flag cannot
        # give. The 6004 channel is mandatory, so local_config is always present.
        if coordinator.local_config is not None:
            for scene in coordinator.local_config.scenes:
                _add(InimSceneActiveBinarySensor(coordinator, scene.id))

        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_sync))
    _sync()


class InimBaseBinarySensor(CoordinatorEntity[InimDataUpdateCoordinator], BinarySensorEntity):
    """Base entity sharing the single panel device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: InimDataUpdateCoordinator) -> None:
        """Initialize the base entity."""
        super().__init__(coordinator)
        self._attr_device_info = panel_device_info(coordinator)


class InimZoneBinarySensor(InimBaseBinarySensor):
    """A binary sensor for a single zone (on = open / in alarm)."""

    def __init__(self, coordinator: InimDataUpdateCoordinator, zone_id: int) -> None:
        """Initialize the zone binary sensor."""
        super().__init__(coordinator)
        self._zone_id = zone_id
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_zone_{zone_id}"
        zone = self._zone
        self._last_label = zone.label if zone is not None else None
        if zone is not None:
            language = label_language(coordinator)
            # Device class -> a state-aware icon, guessed from the initial label.
            self._attr_device_class = guess_device_class(zone.label, language)
            # Group the zone under a per-room device when enabled and a room is
            # recognised in the label; otherwise it stays on the panel device.
            if group_zones_by_room(coordinator):
                room = guess_room(zone.label, language)
                if room is not None:
                    self._attr_device_info = room_device_info(coordinator, room)
        else:
            self._attr_device_class = None

    @property
    def _zone(self) -> Zone | None:
        """Return the current zone model from coordinator data, or None."""
        for zone in self.coordinator.data.zones:
            if zone.id == self._zone_id:
                return zone
        return None

    @property
    def available(self) -> bool:
        """Return whether the zone is still present in coordinator data."""
        return super().available and self._zone is not None

    @property
    def name(self) -> str | None:
        """Return the live zone label, falling back to the last-known one."""
        zone = self._zone
        if zone is not None:
            self._last_label = zone.label
        return self._last_label

    @property
    def is_on(self) -> bool | None:
        """Return True if the zone is open (in alarm)."""
        zone = self._zone
        if zone is None:
            return None
        return zone.state is ZoneState.ALARM

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return zone diagnostic attributes."""
        zone = self._zone
        if zone is None:
            return None
        return {
            "terminal": zone.terminal,
            "excluded": zone.excluded,
            "alarm_memory": zone.alarm_memory,
            "state": zone.state.name,
        }


class InimFaultBinarySensor(InimBaseBinarySensor):
    """A problem binary sensor reflecting any panel fault."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "system_fault"

    def __init__(self, coordinator: InimDataUpdateCoordinator) -> None:
        """Initialize the fault binary sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_fault"

    @property
    def is_on(self) -> bool:
        """Return True if the panel reports any fault."""
        return self.coordinator.data.fault.has_faults

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return fault diagnostic attributes."""
        return {"vcc": self.coordinator.data.fault.vcc}


class InimAreaAlarmMemoryBinarySensor(InimBaseBinarySensor):
    """A problem binary sensor reflecting an area's alarm memory."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: InimDataUpdateCoordinator, area_id: int) -> None:
        """Initialize the area alarm-memory binary sensor."""
        super().__init__(coordinator)
        self._area_id = area_id
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_area_memory_{area_id}"
        area = self._area
        label = area.label if area is not None else None
        self._last_label = label
        # Hide unused, factory-default areas ("AREA 006"..) by default. Gating is
        # fixed at add time from the label the area first had.
        if label is not None and is_factory_default_area(label):
            self._attr_entity_registry_enabled_default = False

    @property
    def _area(self) -> Area | None:
        """Return the current area model from coordinator data, or None."""
        for area in self.coordinator.data.areas:
            if area.id == self._area_id:
                return area
        return None

    @property
    def available(self) -> bool:
        """Return whether the area is still present in coordinator data."""
        return super().available and self._area is not None

    @property
    def name(self) -> str | None:
        """Return the live "<area> alarm memory" name."""
        area = self._area
        if area is not None:
            self._last_label = area.label
        return f"{self._last_label} alarm memory"

    @property
    def is_on(self) -> bool | None:
        """Return True if the area has alarm memory set."""
        area = self._area
        if area is None:
            return None
        return area.alarm_memory


class InimFaultFlagBinarySensor(InimBaseBinarySensor):
    """A PROBLEM binary sensor for a single decomposed fault flag.

    These are diagnostic and almost always off, so they are disabled by default
    to declutter; the summary "System fault" sensor stays the enabled headline.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: InimDataUpdateCoordinator, flag_key: str) -> None:
        """Initialize the per-flag fault binary sensor."""
        super().__init__(coordinator)
        self._flag_key = flag_key
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_fault_{flag_key}"
        # The flag key doubles as the translation key (see the binary_sensor
        # entries in strings.json / translations) so the displayed name is
        # localized rather than hardcoded.
        self._attr_translation_key = flag_key

    @property
    def is_on(self) -> bool:
        """Return True if this specific fault flag is set."""
        return self.coordinator.data.fault.flags.get(self._flag_key, False)


class InimSceneActiveBinarySensor(InimBaseBinarySensor):
    """A RUNNING binary sensor: on when this scenario's arming is active now.

    Computed from the scenario's static partition->mode definition (read once
    over the read-only TCP 6004 channel) compared against live cgi area state,
    so MULTIPLE scenarios can read "on" at the same time — the accurate
    multi-active view the panel's single ``st`` flag cannot provide.
    """

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: InimDataUpdateCoordinator, scene_id: int) -> None:
        """Initialize the multi-active scene sensor."""
        super().__init__(coordinator)
        self._scene_id = scene_id
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_scene_{scene_id}"
        scenario = self._scenario
        self._last_label = scenario.label if scenario is not None else f"Scenario {scene_id}"

    @property
    def _scenario(self) -> Scenario | None:
        """Return the matching cgi scenario (for its live label), or None."""
        for scenario in self.coordinator.data.scenarios:
            if scenario.id == self._scene_id:
                return scenario
        return None

    @property
    def name(self) -> str | None:
        """Return the live scenario label, falling back to the last-known one."""
        scenario = self._scenario
        if scenario is not None:
            self._last_label = scenario.label
        return self._last_label

    @property
    def is_on(self) -> bool:
        """Return True if this scenario's arming matches live area state."""
        return self._scene_id in self.coordinator.active_scene_ids()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the scenario's target partition modes (by area label)."""
        config = self.coordinator.local_config
        if config is None:
            return None
        scene = next((s for s in config.scenes if s.id == self._scene_id), None)
        if scene is None:
            return None
        labels = {area.id: area.label for area in self.coordinator.data.areas}
        return {"arms": {labels.get(p, f"area{p}"): m for p, m in scene.arms.items()}}
