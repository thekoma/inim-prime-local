"""Select platform for the INIM Prime integration."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import ApiStatus, InimApiError, InimConnectionError
from .const import DOMAIN
from .coordinator import InimConfigEntry, InimDataUpdateCoordinator

# Applying a scenario issues a panel write; the cgi is single-threaded, so
# serialize commands to one in flight at a time.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InimConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the INIM Prime select entity."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities([InimScenarioSelect(coordinator)])


class InimScenarioSelect(CoordinatorEntity[InimDataUpdateCoordinator], SelectEntity):
    """Select entity exposing the active scenario."""

    _attr_has_entity_name = True
    _attr_translation_key = "active_scenario"

    def __init__(self, coordinator: InimDataUpdateCoordinator) -> None:
        """Initialize the scenario select."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        version = coordinator.data.version
        self._attr_unique_id = f"{entry.entry_id}_select_scenario"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="INIM",
            model=version.primex,
            sw_version=version.version,
            name=entry.title,
        )

    @property
    def options(self) -> list[str]:
        """Return the list of scenario labels."""
        return [scenario.label for scenario in self.coordinator.data.scenarios]

    @property
    def current_option(self) -> str | None:
        """Return the label of the currently active scenario, if any."""
        for scenario in self.coordinator.data.scenarios:
            if scenario.active:
                return scenario.label
        return None

    async def async_select_option(self, option: str) -> None:
        """Apply the scenario matching the given label."""
        for scenario in self.coordinator.data.scenarios:
            if scenario.label == option:
                try:
                    await self.coordinator.client.apply_scenario(scenario.id)
                except InimApiError as err:
                    if err.status == ApiStatus.ZONES_NOT_READY:
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="zones_not_ready",
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
                await self.coordinator.async_request_refresh()
                return
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_scenario",
            translation_placeholders={"option": option},
        )
