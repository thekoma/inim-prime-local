"""Switch platform for the INIM Prime integration.

Exposes one switch per panel output (relay control) and one bypass switch per
zone (zone exclusion). Output control on a panel without the "Code" configured
returns ``CODE_NOT_ALLOWED`` (status 8); that case is surfaced as a
``HomeAssistantError`` without flipping the optimistic state.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import ApiStatus, InimApiError, InimConnectionError, Output, Zone
from .const import DOMAIN
from .coordinator import InimConfigEntry, InimDataUpdateCoordinator

# Output/bypass toggles issue panel writes; the cgi is single-threaded, so
# serialize commands to one in flight at a time.
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
    # Output control requires a panel Code (otherwise the panel rejects it with
    # CODE_NOT_ALLOWED), so these are decluttered/hidden by default. Reversible
    # by the user from the entity settings.
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
        version = coordinator.data.version
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="INIM",
            model=version.primex,
            sw_version=version.version,
            name=entry.title,
        )

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
        """Return whether the output is active (state != 0)."""
        output = self._output
        if output is None:
            return None
        return output.state != 0

    async def _async_set(self, value: int) -> None:
        """Send a set_output write, translating CODE_NOT_ALLOWED."""
        try:
            await self.coordinator.client.set_output(self._output_id, value)
        except InimApiError as err:
            if err.status == ApiStatus.CODE_NOT_ALLOWED:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="output_code_not_allowed",
                ) from err
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        except InimConnectionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the output on."""
        await self._async_set(1)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the output off."""
        await self._async_set(0)
        await self.coordinator.async_request_refresh()


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
        version = coordinator.data.version
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="INIM",
            model=version.primex,
            sw_version=version.version,
            name=entry.title,
        )

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

    async def _async_set_excluded(self, excluded: bool) -> None:
        """Send a set_zone_excluded write, translating failures."""
        try:
            await self.coordinator.client.set_zone_excluded(self._zone_id, excluded)
        except (InimApiError, InimConnectionError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Bypass (exclude) the zone."""
        await self._async_set_excluded(True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Un-bypass (include) the zone."""
        await self._async_set_excluded(False)
        await self.coordinator.async_request_refresh()
