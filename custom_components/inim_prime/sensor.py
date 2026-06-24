"""Sensor platform for the INIM Prime integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfElectricPotential
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import ZoneState

from .const import DOMAIN
from .coordinator import InimConfigEntry, InimData, InimDataUpdateCoordinator

# Read-only platform: all state comes from the coordinator, no panel writes,
# so updates need not be serialized.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class InimSensorEntityDescription(SensorEntityDescription):
    """Describes an INIM Prime sensor entity."""

    value_fn: Callable[[InimData], StateType]
    available_fn: Callable[[InimData], bool] = lambda _: True


def _active_scenario(data: InimData) -> str:
    """Return the label of the active scenario, or ``none``."""
    return next(
        (scenario.label for scenario in data.scenarios if scenario.active),
        "none",
    )


SENSORS: tuple[InimSensorEntityDescription, ...] = (
    InimSensorEntityDescription(
        key="supply_voltage",
        translation_key="supply_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.fault.vcc,
    ),
    InimSensorEntityDescription(
        key="open_zone_count",
        translation_key="open_zone_count",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: sum(
            1 for zone in data.zones if zone.state is ZoneState.ALARM
        ),
    ),
    InimSensorEntityDescription(
        key="active_scenario",
        translation_key="active_scenario",
        value_fn=_active_scenario,
    ),
    InimSensorEntityDescription(
        key="api_connections",
        translation_key="api_connections",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.api_stats.connections if data.api_stats is not None else None
        ),
        available_fn=lambda data: data.api_stats is not None,
    ),
    InimSensorEntityDescription(
        key="api_last_ip",
        translation_key="api_last_ip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (
            data.api_stats.last_ip if data.api_stats is not None else None
        ),
        available_fn=lambda data: data.api_stats is not None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InimConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the INIM Prime sensors."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        InimSensor(coordinator, entry, description) for description in SENSORS
    )


class InimSensor(CoordinatorEntity[InimDataUpdateCoordinator], SensorEntity):
    """A sensor backed by the INIM Prime coordinator."""

    entity_description: InimSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: InimDataUpdateCoordinator,
        entry: InimConfigEntry,
        description: InimSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_sensor_{description.key}"
        version = coordinator.data.version
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="INIM",
            model=version.primex,
            sw_version=version.version,
            name=entry.title,
        )

    @property
    def native_value(self) -> StateType:
        """Return the current value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        """Return whether the entity is available."""
        return super().available and self.entity_description.available_fn(
            self.coordinator.data
        )
