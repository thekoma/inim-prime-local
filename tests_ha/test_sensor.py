"""Tests for the INIM Prime sensor platform."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import (
    Fault,
    Gsm,
    NativeZoneStatus,
    PanelStatus,
    Power,
    Version,
    Zone,
    ZoneState,
)
from custom_components.inim_prime.const import DOMAIN
from custom_components.inim_prime.coordinator import InimData
from custom_components.inim_prime.sensor import SENSORS, InimSensor


def _build_data(*, zones: list[Zone] | None = None, fault: Fault | None = None) -> InimData:
    """Build a sample InimData payload."""
    return InimData(
        version=Version(raw="4.07 PX500"),
        areas=[],
        zones=zones if zones is not None else [],
        scenarios=[],
        outputs=[],
        fault=fault if fault is not None else Fault(vcc=13.7, has_faults=False),
        power=Power(battery_voltage=13.4),
        panel=PanelStatus(service=False, programming=False),
    )


def _fake_coordinator(data: InimData) -> SimpleNamespace:
    """Return a minimal stand-in for the coordinator."""
    entry = SimpleNamespace(entry_id="abc123", title="INIM Prime")
    return SimpleNamespace(data=data, config_entry=entry, last_update_success=True)


def _make_sensor(key: str, data: InimData) -> InimSensor:
    """Instantiate a single sensor by description key bound to fake state."""
    description = next(d for d in SENSORS if d.key == key)
    coordinator = _fake_coordinator(data)
    return InimSensor(coordinator, coordinator.config_entry, description)  # type: ignore[arg-type]


def _zone(zone_id: int, state: ZoneState) -> Zone:
    return Zone(
        id=zone_id,
        label=f"Zone {zone_id}",
        terminal=zone_id,
        state=state,
        alarm_memory=False,
        excluded=False,
    )


def test_supply_voltage() -> None:
    """Supply voltage maps to fault.vcc with V unit and voltage device class."""
    sensor = _make_sensor("supply_voltage", _build_data(fault=Fault(vcc=12.3, has_faults=False)))
    assert sensor.native_value == 12.3
    assert sensor.native_unit_of_measurement == "V"
    assert sensor.device_class == "voltage"
    assert sensor.state_class == "measurement"
    assert sensor.unique_id == "abc123_sensor_supply_voltage"
    assert sensor.available is True


def test_open_zone_count() -> None:
    """Open-zone count is the number of zones in the ALARM state."""
    zones = [
        _zone(1, ZoneState.ALARM),
        _zone(2, ZoneState.READY),
        _zone(3, ZoneState.ALARM),
        _zone(4, ZoneState.FAULT),
    ]
    sensor = _make_sensor("open_zone_count", _build_data(zones=zones))
    assert sensor.native_value == 2


def test_only_native_sensors() -> None:
    """The cgi-only API sensors (version, connections, last client) are gone."""
    assert {d.key for d in SENSORS} == {
        "supply_voltage",
        "open_zone_count",
        "battery_voltage",
        "battery_temperature",
        "battery_resistance",
        "gsm_signal",
    }


def test_gsm_signal_sensor() -> None:
    """The GSM signal shows the level, with the communicator details as attributes."""
    gsm = Gsm(
        state="ok",
        signal=67,
        networks=("2G", "3G", "4G"),
        operator_code="00101",
        volte=True,
        faults={"roaming": True, "low_signal": False},
    )
    sensor = _make_sensor("gsm_signal", replace(_build_data(), gsm=gsm))
    assert (sensor.native_value, sensor.native_unit_of_measurement) == (67, "%")
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.extra_state_attributes == {
        "state": "ok",
        "networks": "2G-3G-4G",
        "operator_code": "00101",
        "volte": True,
        "faults": ["roaming"],
    }
    unregistered = replace(gsm, state="not_initialised", signal=None, operator_code="")
    sensor = _make_sensor("gsm_signal", replace(_build_data(), gsm=unregistered))
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["operator_code"] is None


def test_gsm_signal_without_gsm_data() -> None:
    """Without GSM data the sensor is not created, and reads nothing."""
    description = next(d for d in SENSORS if d.key == "gsm_signal")
    data = _build_data()
    assert not description.exists_fn(data)
    sensor = _make_sensor("gsm_signal", data)
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None
    other = next(d for d in SENSORS if d.key == "supply_voltage")
    assert other.exists_fn(data)
    assert _make_sensor("supply_voltage", data).extra_state_attributes is None


def test_battery_sensors() -> None:
    """Battery voltage, temperature and internal resistance come from data.power."""
    data = replace(
        _build_data(),
        power=Power(battery_voltage=13.2, battery_temperature=27, battery_resistance=115),
    )
    voltage = _make_sensor("battery_voltage", data)
    assert (voltage.native_value, voltage.native_unit_of_measurement) == (13.2, "V")
    assert voltage.entity_category is None
    temperature = _make_sensor("battery_temperature", data)
    assert (temperature.native_value, temperature.native_unit_of_measurement) == (27, "°C")
    assert temperature.device_class == "temperature"
    assert temperature.entity_category is EntityCategory.DIAGNOSTIC
    resistance = _make_sensor("battery_resistance", data)
    assert (resistance.native_value, resistance.native_unit_of_measurement) == (115, "mΩ")
    assert resistance.device_class is None


def test_battery_values_not_read_yet() -> None:
    """Before the power values are read, temperature and resistance are unknown."""
    data = _build_data()
    assert _make_sensor("battery_temperature", data).native_value is None
    assert _make_sensor("battery_resistance", data).native_value is None


def test_device_info_shared() -> None:
    """All sensors share the one device identified by the entry id."""
    sensor = _make_sensor("supply_voltage", _build_data())
    assert sensor.device_info["identifiers"] == {("inim_prime", "abc123")}
    assert sensor.device_info["manufacturer"] == "INIM"
    # Model and firmware come from the native version string "4.07 PX500".
    assert sensor.device_info["model"] == "PX500"
    assert sensor.device_info["sw_version"] == "4.07"
    assert sensor.device_info["name"] == "INIM Prime"


async def test_setup_reads_the_live_values(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    zone_statuses: dict[int, NativeZoneStatus],
    make_system_status,
) -> None:
    """End to end: the sensors follow the native system and zone statuses."""
    patch_client.async_get_system_status.return_value = make_system_status(supply=24.3)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    entry_id = mock_config_entry.entry_id
    supply = registry.async_get_entity_id("sensor", DOMAIN, f"{entry_id}_sensor_supply_voltage")
    open_zones = registry.async_get_entity_id("sensor", DOMAIN, f"{entry_id}_sensor_open_zone_count")
    assert hass.states.get(supply).state == "24.3"
    assert hass.states.get(open_zones).state == "0"

    zone_statuses[1] = NativeZoneStatus(state=ZoneState.ALARM, excluded=False, alarm_memory=False)
    await mock_config_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(open_zones).state == "1"
