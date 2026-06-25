"""Tests for the INIM Prime binary_sensor platform."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from custom_components.inim_prime.client import (
    AreaMode,
    AreaState,
    ZoneState,
    Area,
    Fault,
    Scenario,
    Version,
    Zone,
)

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import EntityCategory

from custom_components.inim_prime.binary_sensor import (
    InimAreaAlarmMemoryBinarySensor,
    InimFaultBinarySensor,
    InimFaultFlagBinarySensor,
    InimScenarioBinarySensor,
    InimZoneBinarySensor,
    _label_language,
)
from custom_components.inim_prime.const import CONF_LABEL_LANGUAGE
from custom_components.inim_prime.coordinator import InimData


def _zone(zone_id: int, label: str, state: ZoneState, **kw) -> Zone:
    return Zone(
        id=zone_id,
        label=label,
        terminal=kw.get("terminal", 5),
        state=state,
        alarm_memory=kw.get("alarm_memory", False),
        excluded=kw.get("excluded", False),
    )


def _area(area_id: int, label: str, *, alarm_memory: bool) -> Area:
    return Area(
        id=area_id,
        label=label,
        mode=AreaMode.DISARMED,
        state=AreaState.READY,
        alarm_memory=alarm_memory,
    )


@pytest.fixture
def inim_data() -> InimData:
    """Return a sample InimData."""
    return InimData(
        version=Version(
            version="4.07", verhttp="1.0", primex="4.07 PX500", servizio=False
        ),
        areas=[
            _area(1, "Ground", alarm_memory=True),
            _area(2, "Upstairs", alarm_memory=False),
        ],
        zones=[
            _zone(1, "Porta ingresso", ZoneState.ALARM, terminal=3, excluded=True),
            _zone(2, "Finestra cucina", ZoneState.READY),
            _zone(3, "Volumetrico salotto", ZoneState.READY),
            _zone(4, "Sirena esterna", ZoneState.READY),
            _zone(5, "Generic input", ZoneState.READY),
        ],
        scenarios=[
            Scenario(id=0, label="Disarm all", active=True),
            Scenario(id=1, label="Arm away", active=False),
        ],
        outputs=[],
        fault=Fault(vcc=13.7, raw_fau="0", has_faults=False),
        api_stats=None,
    )


@pytest.fixture
def coordinator(inim_data: InimData) -> SimpleNamespace:
    """Return a fake coordinator with sample data.

    Includes ``hass.config.language`` and ``config_entry.options`` because the
    zone entity resolves the label-guessing language from them.
    """
    entry = SimpleNamespace(entry_id="abc123", title="INIM Prime", options={})
    hass = SimpleNamespace(config=SimpleNamespace(language="it"))
    return SimpleNamespace(
        data=inim_data,
        config_entry=entry,
        hass=hass,
        last_update_success=True,
    )


def test_label_language_resolution() -> None:
    """The label language follows the override option, else the HA language."""
    hass = SimpleNamespace(config=SimpleNamespace(language="it"))

    auto = SimpleNamespace(
        hass=hass, config_entry=SimpleNamespace(options={})
    )
    assert _label_language(auto) == "it"  # no option -> HA language

    explicit_auto = SimpleNamespace(
        hass=hass, config_entry=SimpleNamespace(options={CONF_LABEL_LANGUAGE: "auto"})
    )
    assert _label_language(explicit_auto) == "it"  # "auto" -> HA language

    override = SimpleNamespace(
        hass=hass, config_entry=SimpleNamespace(options={CONF_LABEL_LANGUAGE: "en"})
    )
    assert _label_language(override) == "en"  # explicit override wins


def test_zone_open_maps_to_is_on(coordinator: SimpleNamespace) -> None:
    """A zone in ALARM is on; a READY zone is off."""
    open_zone = InimZoneBinarySensor(coordinator, 1)
    closed_zone = InimZoneBinarySensor(coordinator, 2)

    assert open_zone.is_on is True
    assert closed_zone.is_on is False


def test_zone_unique_id_and_device_class(coordinator: SimpleNamespace) -> None:
    """Zone entity carries the unique_id pattern and guessed device class."""
    zone = InimZoneBinarySensor(coordinator, 1)
    assert zone.unique_id == "abc123_zone_1"
    assert zone.device_class is BinarySensorDeviceClass.DOOR
    assert zone.name == "Porta ingresso"


def test_zone_attributes(coordinator: SimpleNamespace) -> None:
    """Zone exposes terminal/excluded/alarm_memory/state attributes."""
    zone = InimZoneBinarySensor(coordinator, 1)
    attrs = zone.extra_state_attributes
    assert attrs == {
        "terminal": 3,
        "excluded": True,
        "alarm_memory": False,
        "state": "ALARM",
    }


def test_zone_shares_device_info(coordinator: SimpleNamespace) -> None:
    """All entities share the single panel DeviceInfo."""
    zone = InimZoneBinarySensor(coordinator, 1)
    info = zone.device_info
    assert info["identifiers"] == {("inim_prime", "abc123")}
    assert info["manufacturer"] == "INIM"
    assert info["model"] == "4.07 PX500"
    assert info["sw_version"] == "4.07"
    assert info["name"] == "INIM Prime"


def test_fault_binary_sensor(coordinator: SimpleNamespace) -> None:
    """Fault sensor is a PROBLEM class reflecting has_faults with vcc attr."""
    fault = InimFaultBinarySensor(coordinator)
    assert fault.device_class is BinarySensorDeviceClass.PROBLEM
    assert fault.unique_id == "abc123_fault"
    assert fault.is_on is False
    assert fault.extra_state_attributes == {"vcc": 13.7}

    # Fault is frozen; swap the whole InimData to simulate a new poll.
    coordinator.data = InimData(
        version=coordinator.data.version,
        areas=coordinator.data.areas,
        zones=coordinator.data.zones,
        scenarios=[],
        outputs=[],
        fault=Fault(vcc=11.2, raw_fau="4", has_faults=True),
        api_stats=None,
    )
    assert fault.is_on is True
    assert fault.extra_state_attributes == {"vcc": 11.2}


def test_area_alarm_memory_binary_sensor(coordinator: SimpleNamespace) -> None:
    """Per-area alarm-memory sensor is a PROBLEM class on area.alarm_memory."""
    on_area = InimAreaAlarmMemoryBinarySensor(coordinator, 1)
    off_area = InimAreaAlarmMemoryBinarySensor(coordinator, 2)

    assert on_area.device_class is BinarySensorDeviceClass.PROBLEM
    assert on_area.unique_id == "abc123_area_memory_1"
    assert on_area.name == "Ground alarm memory"
    assert on_area.is_on is True
    assert off_area.is_on is False


def test_area_alarm_memory_enabled_default(inim_data: InimData) -> None:
    """Factory-default areas are hidden by default; real ones stay enabled."""
    entry = SimpleNamespace(entry_id="abc123", title="INIM Prime")
    data = InimData(
        version=inim_data.version,
        areas=[
            _area(6, "AREA       006 ", alarm_memory=False),
            _area(7, "Box", alarm_memory=False),
        ],
        zones=[],
        scenarios=[],
        outputs=[],
        fault=inim_data.fault,
        api_stats=None,
    )
    coordinator = SimpleNamespace(
        data=data, config_entry=entry, last_update_success=True
    )

    factory = InimAreaAlarmMemoryBinarySensor(coordinator, 6)
    real = InimAreaAlarmMemoryBinarySensor(coordinator, 7)

    assert factory.entity_registry_enabled_default is False
    assert real.entity_registry_enabled_default is True


def test_fault_flag_binary_sensor(coordinator: SimpleNamespace) -> None:
    """A per-flag fault sensor reflects its flag; disabled-by-default + DIAGNOSTIC."""
    sensor = InimFaultFlagBinarySensor(coordinator, "low_battery")

    assert sensor.device_class is BinarySensorDeviceClass.PROBLEM
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.entity_registry_enabled_default is False
    assert sensor.unique_id == "abc123_fault_low_battery"
    # Fixed-name entity: name comes from the translation; the flag key doubles
    # as the translation key.
    assert sensor.translation_key == "low_battery"
    assert sensor.is_on is False

    # Swap in a poll where the low_battery flag is set.
    coordinator.data = InimData(
        version=coordinator.data.version,
        areas=coordinator.data.areas,
        zones=coordinator.data.zones,
        scenarios=coordinator.data.scenarios,
        outputs=[],
        fault=Fault(
            vcc=11.2,
            raw_fau="4",
            has_faults=True,
            flags={"low_battery": True},
        ),
        api_stats=None,
    )
    assert sensor.is_on is True


def test_scenario_binary_sensor(coordinator: SimpleNamespace) -> None:
    """A per-scenario sensor tracks Scenario.active; RUNNING, disabled by default.

    Disabled by default because the panel only sets a scenario's ``st`` flag for
    the system-wide Total macro, not for single-area scenarios (see the
    InimScenarioBinarySensor docstring); the per-area alarm_control_panel is the
    authoritative arm state.
    """
    active = InimScenarioBinarySensor(coordinator, 0)
    inactive = InimScenarioBinarySensor(coordinator, 1)

    assert active.device_class is BinarySensorDeviceClass.RUNNING
    assert active.entity_category is None
    assert active.entity_registry_enabled_default is False
    assert active.unique_id == "abc123_scenario_active_0"
    assert active.name == "Disarm all"
    assert active.is_on is True
    assert active.available is True
    assert inactive.is_on is False

    # A new poll flips the active scenario.
    coordinator.data = InimData(
        version=coordinator.data.version,
        areas=coordinator.data.areas,
        zones=coordinator.data.zones,
        scenarios=[
            Scenario(id=0, label="Disarm all", active=False),
            Scenario(id=1, label="Arm away", active=True),
        ],
        outputs=[],
        fault=coordinator.data.fault,
        api_stats=None,
    )
    assert active.is_on is False
    assert inactive.is_on is True

    # When the scenario disappears from the panel, the entity goes unavailable.
    coordinator.data = InimData(
        version=coordinator.data.version,
        areas=coordinator.data.areas,
        zones=coordinator.data.zones,
        scenarios=[],
        outputs=[],
        fault=coordinator.data.fault,
        api_stats=None,
    )
    assert active.available is False
    assert active.is_on is None
