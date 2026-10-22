"""Tests for the INIM Prime sensor platform."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import (
    Fault,
    NativeZoneStatus,
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
        version=Version(raw="4.07 PX500", service=False),
        areas=[],
        zones=zones if zones is not None else [],
        scenarios=[],
        outputs=[],
        fault=fault if fault is not None else Fault(vcc=13.7, has_faults=False),
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
    assert {d.key for d in SENSORS} == {"supply_voltage", "open_zone_count"}


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
