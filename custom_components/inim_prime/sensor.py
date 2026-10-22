"""Sensor platform for the INIM Prime integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import Gsm, ZoneState
from .coordinator import InimConfigEntry, InimData, InimDataUpdateCoordinator
from .device import panel_device_info

# Read-only platform: all state comes from the coordinator, no panel writes,
# so updates need not be serialized.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class InimSensorEntityDescription(SensorEntityDescription):
    """Describes an INIM Prime sensor entity."""

    value_fn: Callable[[InimData], StateType]
    # Whether the sensor exists on this panel (checked once, at setup).
    exists_fn: Callable[[InimData], bool] = lambda _: True
    attributes_fn: Callable[[InimData], dict[str, Any] | None] = lambda _: None


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
        key="battery_voltage",
        translation_key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.power.battery_voltage,
    ),
    InimSensorEntityDescription(
        key="battery_temperature",
        translation_key="battery_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.power.battery_temperature,
    ),
    InimSensorEntityDescription(
        key="battery_resistance",
        translation_key="battery_resistance",
        # Home Assistant has no resistance device class.
        native_unit_of_measurement="mΩ",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.power.battery_resistance,
    ),
    InimSensorEntityDescription(
        key="gsm_signal",
        translation_key="gsm_signal",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.gsm.signal if data.gsm is not None else None,
        exists_fn=lambda data: data.gsm is not None,
        attributes_fn=lambda data: _gsm_attributes(data.gsm),
    ),
    InimSensorEntityDescription(
        key="open_zone_count",
        translation_key="open_zone_count",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: sum(
            1 for zone in data.zones if zone.state is ZoneState.ALARM
        ),
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
        InimSensor(coordinator, entry, description)
        for description in SENSORS
        if description.exists_fn(coordinator.data)
    )


def _gsm_attributes(gsm: Gsm | None) -> dict[str, Any] | None:
    """Return the LTE communicator details shown with its signal level."""
    if gsm is None:
        return None
    return {
        "state": gsm.state,
        "networks": "-".join(gsm.networks),
        "operator_code": gsm.operator_code or None,
        "volte": gsm.volte,
        "faults": sorted(name for name, on in gsm.faults.items() if on),
    }


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
        self._attr_device_info = panel_device_info(coordinator)

    @property
    def native_value(self) -> StateType:
        """Return the current value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the sensor's details, if it has any."""
        return self.entity_description.attributes_fn(self.coordinator.data)
