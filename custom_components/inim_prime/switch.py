"""Switch platform for the INIM Prime integration.

Exposes one switch per panel output (relay control) and one bypass switch per
zone (zone exclusion), except for zones programmed "Non escludibile".
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import Output, Zone
from .commands import async_set_output, async_set_zone_excluded
from .const import DOMAIN
from .coordinator import InimConfigEntry, InimDataUpdateCoordinator
from .device import (
    group_zones_by_room,
    label_language,
    panel_device_info,
    room_device_info,
)
from .room_guess import guess_room
from .zone_kind import native_zone

# Output/bypass toggles are panel writes: serialize them to one in flight at a
# time.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InimConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the INIM Prime switch entities.

    Outputs and per-zone bypass switches are added dynamically via a coordinator
    listener so objects appearing after setup show up without a reload. The
    output declutter gating (disabled-by-default) is a class attribute, so it
    applies to dynamically-added output switches too.
    """
    coordinator = entry.runtime_data.coordinator

    def _bypassable(zone_id: int) -> bool:
        native = native_zone(coordinator, zone_id)
        return native is None or native.bypassable

    # A "Non escludibile" zone cannot be bypassed: it gets no bypass switch, and
    # loses one created before its programming was known.
    for zone in coordinator.data.zones:
        if not _bypassable(zone.id):
            registry = er.async_get(hass)
            uid = f"{entry.entry_id}_zone_bypass_{zone.id}"
            if (entity_id := registry.async_get_entity_id("switch", DOMAIN, uid)) is not None:
                registry.async_remove(entity_id)

    # A single per-panel preference switch (not backed by panel data).
    async_add_entities([InimForceArmSwitch(coordinator)])

    known: set[str] = set()

    @callback
    def _sync() -> None:
        """Add switches for output/zone ids not yet known."""
        new_entities: list[SwitchEntity] = []
        for output in coordinator.data.outputs:
            entity: SwitchEntity = InimOutputSwitch(coordinator, entry, output.id)
            uid = entity.unique_id
            if uid is None or uid in known:
                continue
            known.add(uid)
            new_entities.append(entity)
        for zone in coordinator.data.zones:
            if not _bypassable(zone.id):
                continue
            entity = InimZoneBypassSwitch(coordinator, entry, zone.id)
            uid = entity.unique_id
            if uid is None or uid in known:
                continue
            known.add(uid)
            new_entities.append(entity)
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_sync))
    _sync()


class InimOutputSwitch(CoordinatorEntity[InimDataUpdateCoordinator], SwitchEntity):
    """Switch controlling a single panel output (relay)."""

    _attr_has_entity_name = True
    # Outputs drive sirens and other hardware, so these are decluttered/hidden by
    # default. Reversible by the user from the entity settings.
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: InimDataUpdateCoordinator,
        entry: InimConfigEntry,
        output_id: int,
    ) -> None:
        """Initialize the output switch."""
        super().__init__(coordinator)
        self._output_id = output_id
        self._attr_unique_id = f"{entry.entry_id}_output_{output_id}"
        self._attr_device_info = panel_device_info(coordinator)

    @property
    def _output(self) -> Output | None:
        """Return the backing Output model from the latest coordinator data."""
        for output in self.coordinator.data.outputs:
            if output.id == self._output_id:
                return output
        return None

    @property
    def available(self) -> bool:
        """Return True if the output is still present in coordinator data."""
        return super().available and self._output is not None

    @property
    def name(self) -> str | None:
        """Return the output label."""
        output = self._output
        return output.label if output is not None else None

    @property
    def is_on(self) -> bool | None:
        """Return whether the output is active (state != 0), None if unknown."""
        output = self._output
        if output is None or output.state is None:
            return None
        return output.state != 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the output on (a failure raises, see commands.py)."""
        await async_set_output(self.coordinator, self._output_id, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the output off (a failure raises, see commands.py)."""
        await async_set_output(self.coordinator, self._output_id, 0)


class InimZoneBypassSwitch(
    CoordinatorEntity[InimDataUpdateCoordinator], SwitchEntity
):
    """Switch that bypasses (excludes) a single zone."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: InimDataUpdateCoordinator,
        entry: InimConfigEntry,
        zone_id: int,
    ) -> None:
        """Initialize the bypass switch."""
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._attr_unique_id = f"{entry.entry_id}_zone_bypass_{zone_id}"
        self._attr_device_info = panel_device_info(coordinator)
        # Follow the zone into its per-room device so the bypass switch lives
        # alongside the zone's binary_sensor (same room grouping).
        if group_zones_by_room(coordinator):
            zone = self._zone
            if zone is not None:
                room = guess_room(zone.label, label_language(coordinator))
                if room is not None:
                    self._attr_device_info = room_device_info(coordinator, room)

    @property
    def _zone(self) -> Zone | None:
        """Return the backing Zone model from the latest coordinator data."""
        for zone in self.coordinator.data.zones:
            if zone.id == self._zone_id:
                return zone
        return None

    @property
    def available(self) -> bool:
        """Return True if the zone is still present in coordinator data."""
        return super().available and self._zone is not None

    @property
    def name(self) -> str | None:
        """Return the bypass switch name."""
        zone = self._zone
        return f"{zone.label} bypass" if zone is not None else None

    @property
    def is_on(self) -> bool | None:
        """Return whether the zone is excluded (bypassed)."""
        zone = self._zone
        if zone is None:
            return None
        return zone.excluded

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Bypass (exclude) the zone (a failure raises, see commands.py)."""
        await async_set_zone_excluded(self.coordinator, self._zone_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Un-bypass (include) the zone (a failure raises, see commands.py)."""
        await async_set_zone_excluded(self.coordinator, self._zone_id, False)


class InimForceArmSwitch(RestoreEntity, SwitchEntity):
    """Preference switch: force-arm (bypass open zones) for the scenario buttons.

    A local, persisted toggle (not a panel write). When on, pressing an
    apply-scenario button bypasses the open zones instead of failing. The state
    is mirrored onto the coordinator so the buttons can read it, and restored
    across restarts via RestoreEntity.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "force_arm_on_open"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: InimDataUpdateCoordinator) -> None:
        """Initialize the force-arm preference switch."""
        self.coordinator = coordinator
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_force_arm_on_open"
        self._attr_device_info = panel_device_info(coordinator)
        self._attr_is_on = coordinator.force_arm_on_open

    async def async_added_to_hass(self) -> None:
        """Restore the last on/off state and mirror it onto the coordinator."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self._attr_is_on = last.state == "on"
        self.coordinator.force_arm_on_open = bool(self._attr_is_on)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable force arming on open zones."""
        self._attr_is_on = True
        self.coordinator.force_arm_on_open = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable force arming on open zones."""
        self._attr_is_on = False
        self.coordinator.force_arm_on_open = False
        self.async_write_ha_state()
