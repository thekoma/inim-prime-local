"""Tests for the INIM Prime binary_sensor platform."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.binary_sensor import (
    InimAreaAlarmMemoryBinarySensor,
    InimFaultBinarySensor,
    InimFaultFlagBinarySensor,
    InimZoneBinarySensor,
)
from custom_components.inim_prime.client import (
    Area,
    AreaMode,
    AreaState,
    Fault,
    Local6004Config,
    Local6004Structure,
    NativeZoneStatus,
    Scenario,
    Version,
    Zone,
    ZoneState,
)
from custom_components.inim_prime.const import CONF_GROUP_BY_ROOM, CONF_LABEL_LANGUAGE, DOMAIN
from custom_components.inim_prime.coordinator import InimData
from custom_components.inim_prime.device import label_language


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
        version=Version(raw="4.07 PX500", service=False),
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
        fault=Fault(vcc=13.7, has_faults=False),
    )


def _local_config() -> Local6004Config:
    """A static config without zone definitions: zone kinds come from labels."""
    return Local6004Config(firmware="4.07 PX500", layout_ok=True)


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
        local_config=_local_config(),
        structure=Local6004Structure(),
        last_update_success=True,
    )


def test_label_language_resolution() -> None:
    """The label language follows the override option, else the HA language."""
    hass = SimpleNamespace(config=SimpleNamespace(language="it"))

    auto = SimpleNamespace(hass=hass, config_entry=SimpleNamespace(options={}))
    assert label_language(auto) == "it"  # no option -> HA language

    explicit_auto = SimpleNamespace(
        hass=hass, config_entry=SimpleNamespace(options={CONF_LABEL_LANGUAGE: "auto"})
    )
    assert label_language(explicit_auto) == "it"  # "auto" -> HA language

    override = SimpleNamespace(
        hass=hass, config_entry=SimpleNamespace(options={CONF_LABEL_LANGUAGE: "en"})
    )
    assert label_language(override) == "en"  # explicit override wins


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


def test_zone_without_room_uses_panel_device(coordinator: SimpleNamespace) -> None:
    """A zone whose label has no recognised room stays on the panel device."""
    zone = InimZoneBinarySensor(coordinator, 5)  # "Generic input" -> no room
    info = zone.device_info
    assert info["identifiers"] == {("inim_prime", "abc123")}
    assert info["manufacturer"] == "INIM"
    assert info["model"] == "PX500"
    assert info["sw_version"] == "4.07"
    assert info["name"] == "INIM Prime"


def test_zone_grouped_into_room_device(coordinator: SimpleNamespace) -> None:
    """With grouping on, a zone is placed under its per-room sub-device."""
    zone = InimZoneBinarySensor(coordinator, 1)  # "Porta ingresso" -> Ingresso
    info = zone.device_info
    assert info["identifiers"] == {("inim_prime", "abc123_room_ingresso")}
    assert info["name"] == "Ingresso"
    assert info["manufacturer"] == "INIM"
    assert info["via_device"] == ("inim_prime", "abc123")


def test_zone_grouping_disabled_uses_panel_device(inim_data: InimData) -> None:
    """With grouping disabled, even a roomful zone stays on the panel device."""
    entry = SimpleNamespace(
        entry_id="abc123",
        title="INIM Prime",
        options={CONF_GROUP_BY_ROOM: False},
    )
    coord = SimpleNamespace(
        data=inim_data,
        config_entry=entry,
        hass=SimpleNamespace(config=SimpleNamespace(language="it")),
        local_config=_local_config(),
        structure=Local6004Structure(),
        last_update_success=True,
    )
    zone = InimZoneBinarySensor(coord, 1)  # "Porta ingresso"
    assert zone.device_info["identifiers"] == {("inim_prime", "abc123")}


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
        fault=Fault(vcc=11.2, has_faults=True),
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
    """Enabled whatever the label: the area selection decides if it exists."""
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
    )
    coordinator = SimpleNamespace(
        data=data, config_entry=entry, local_config=_local_config(), last_update_success=True
    )

    factory = InimAreaAlarmMemoryBinarySensor(coordinator, 6)
    real = InimAreaAlarmMemoryBinarySensor(coordinator, 7)

    assert factory.entity_registry_enabled_default is True
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
        fault=Fault(vcc=11.2, has_faults=True, flags={"low_battery": True}),
    )
    assert sensor.is_on is True


def test_zone_missing_from_the_data(coordinator: SimpleNamespace) -> None:
    """A zone sensor whose zone is gone is unavailable and reports nothing."""
    zone = InimZoneBinarySensor(coordinator, 99)
    assert zone.device_class is None
    assert zone.available is False
    assert zone.name is None
    assert zone.is_on is None
    assert zone.extra_state_attributes is None


def test_area_alarm_memory_missing_area(coordinator: SimpleNamespace) -> None:
    """An alarm-memory sensor whose area is gone keeps its last name, reports nothing."""
    sensor = InimAreaAlarmMemoryBinarySensor(coordinator, 1)
    assert sensor.available is True
    coordinator.data = InimData(
        version=coordinator.data.version,
        areas=[],
        zones=coordinator.data.zones,
        scenarios=coordinator.data.scenarios,
        outputs=[],
        fault=coordinator.data.fault,
    )
    assert sensor.available is False
    assert sensor.is_on is None
    assert sensor.name == "Ground alarm memory"


async def test_setup_creates_the_sensors_and_follows_the_zone(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    zone_statuses: dict[int, NativeZoneStatus],
    make_system_status,
) -> None:
    """End to end: zone, fault, alarm-memory and scene sensors follow the live reads."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    entry_id = mock_config_entry.entry_id

    def _state(unique_id: str) -> str:
        entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, f"{entry_id}_{unique_id}")
        assert entity_id is not None, unique_id
        return hass.states.get(entity_id).state

    assert _state("zone_1") == "off"
    assert _state("fault") == "off"
    assert _state("area_memory_1") == "off"
    assert _state("scene_1") == "on"
    # The per-flag fault sensors exist (disabled by default, so no state).
    assert registry.async_get_entity_id("binary_sensor", DOMAIN, f"{entry_id}_fault_tamper")

    zone_statuses[1] = NativeZoneStatus(state=ZoneState.ALARM, excluded=False, alarm_memory=True)
    patch_client.async_get_system_status.return_value = make_system_status(fault_mains=True)
    await mock_config_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _state("zone_1") == "on"
    assert _state("fault") == "on"
