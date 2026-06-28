"""Tests for the INIM Prime sensor platform."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.inim_prime.client import (
    ApiStats,
    Fault,
    Scenario,
    Version,
    Zone,
    ZoneState,
)
from custom_components.inim_prime.coordinator import InimData
from custom_components.inim_prime.sensor import SENSORS, InimSensor


def _build_data(
    *,
    api_stats: ApiStats | None = None,
    zones: list[Zone] | None = None,
    scenarios: list[Scenario] | None = None,
    fault: Fault | None = None,
) -> InimData:
    """Build a sample InimData payload."""
    return InimData(
        version=Version(version="4.07", verhttp="1.0", primex="4.07 PX500", servizio=False),
        areas=[],
        zones=zones if zones is not None else [],
        scenarios=scenarios if scenarios is not None else [],
        outputs=[],
        fault=fault if fault is not None else Fault(vcc=13.7, raw_fau="0", has_faults=False),
        api_stats=api_stats,
    )


def _fake_coordinator(data: InimData) -> SimpleNamespace:
    """Return a minimal stand-in for the coordinator."""
    return SimpleNamespace(data=data, last_update_success=True)


def _make_sensor(key: str, data: InimData) -> InimSensor:
    """Instantiate a single sensor by description key bound to fake state."""
    description = next(d for d in SENSORS if d.key == key)
    coordinator = _fake_coordinator(data)
    entry = SimpleNamespace(entry_id="abc123", title="INIM Prime")
    return InimSensor(coordinator, entry, description)  # type: ignore[arg-type]


def test_supply_voltage() -> None:
    """Supply voltage maps to fault.vcc with V unit and voltage device class."""
    sensor = _make_sensor(
        "supply_voltage",
        _build_data(fault=Fault(vcc=12.3, raw_fau="0", has_faults=False)),
    )
    assert sensor.native_value == 12.3
    assert sensor.native_unit_of_measurement == "V"
    assert sensor.device_class == "voltage"
    assert sensor.state_class == "measurement"
    assert sensor.unique_id == "abc123_sensor_supply_voltage"
    assert sensor.available is True


def test_open_zone_count() -> None:
    """Open-zone count is the number of zones in the ALARM state."""
    zones = [
        Zone(
            id=1, label="A", terminal=1, state=ZoneState.ALARM, alarm_memory=False, excluded=False
        ),
        Zone(
            id=2, label="B", terminal=2, state=ZoneState.READY, alarm_memory=False, excluded=False
        ),
        Zone(
            id=3, label="C", terminal=3, state=ZoneState.ALARM, alarm_memory=False, excluded=False
        ),
        Zone(
            id=4, label="D", terminal=4, state=ZoneState.FAULT, alarm_memory=False, excluded=False
        ),
    ]
    sensor = _make_sensor("open_zone_count", _build_data(zones=zones))
    assert sensor.native_value == 2


def test_api_connections_with_stats() -> None:
    """API connections is diagnostic, available, and reports the count."""
    stats = ApiStats(api="primex", connections=7, last_connection="x", last_ip="192.0.2.9")
    sensor = _make_sensor("api_connections", _build_data(api_stats=stats))
    assert sensor.native_value == 7
    assert sensor.entity_category == "diagnostic"
    assert sensor.available is True


def test_api_last_ip_with_stats() -> None:
    """Last API IP is diagnostic and reports the address."""
    stats = ApiStats(api="primex", connections=7, last_connection="x", last_ip="192.0.2.9")
    sensor = _make_sensor("api_last_ip", _build_data(api_stats=stats))
    assert sensor.native_value == "192.0.2.9"
    assert sensor.entity_category == "diagnostic"
    assert sensor.available is True


@pytest.mark.parametrize("key", ["api_connections", "api_last_ip"])
def test_api_sensors_unavailable_without_stats(key: str) -> None:
    """Diagnostic API sensors are unavailable when api_stats is None."""
    sensor = _make_sensor(key, _build_data(api_stats=None))
    assert sensor.available is False
    assert sensor.native_value is None


def test_device_info_shared() -> None:
    """All sensors share the one device identified by the entry id."""
    sensor = _make_sensor("supply_voltage", _build_data())
    assert sensor.device_info["identifiers"] == {("inim_prime", "abc123")}
    assert sensor.device_info["manufacturer"] == "INIM"
    assert sensor.device_info["model"] == "4.07 PX500"
    assert sensor.device_info["sw_version"] == "4.07"
    assert sensor.device_info["name"] == "INIM Prime"
