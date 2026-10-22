"""Tests for the INIM Prime alarm_control_panel platform."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.alarm_control_panel import (
    InimAlarmControlPanel,
)
from custom_components.inim_prime.client import (
    Area,
    AreaMode,
    AreaState,
    Fault,
    Local6004Config,
    Local6004Error,
    NativeAreaStatus,
    NativeCodeCheck,
    NativeCommandNotSent,
    NativeZonesNotReady,
    PanelStatus,
    Power,
    Version,
)
from custom_components.inim_prime.const import (
    CONF_LOCAL_PASSWORD,
    CONF_REQUIRE_CODE,
    DOMAIN,
)
from custom_components.inim_prime.coordinator import (
    InimData,
    InimDataUpdateCoordinator,
)


def _area(
    mode: AreaMode,
    state: AreaState = AreaState.READY,
    alarm_memory: bool = False,
    area_id: int = 1,
    label: str = "Home",
) -> Area:
    return Area(
        id=area_id,
        label=label,
        mode=mode,
        state=state,
        alarm_memory=alarm_memory,
    )


@pytest.fixture
def entity_factory(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
):
    """Build an entity bound to a coordinator carrying the given area."""
    mock_config_entry.add_to_hass(hass)

    def _factory(area: Area, entity_area_id: int | None = None) -> InimAlarmControlPanel:
        coordinator = InimDataUpdateCoordinator(
            hass, mock_config_entry, mock_local_client, mock_local_config
        )
        coordinator.data = InimData(
            version=Version(raw="4.07 PX500"),
            areas=[area],
            zones=[],
            scenarios=[],
            outputs=[],
            fault=Fault(vcc=13.7, has_faults=False),
            power=Power(battery_voltage=13.4),
            panel=PanelStatus(service=False, programming=False),
        )
        # The command re-reads the panel right after it; not under test here.
        coordinator.async_refresh = AsyncMock()
        entity = InimAlarmControlPanel(coordinator, entity_area_id or area.id)
        entity.hass = hass
        entity.entity_id = "alarm_control_panel.home"
        return entity

    return _factory


@pytest.mark.parametrize(
    ("area", "expected"),
    [
        (_area(AreaMode.DISARMED), AlarmControlPanelState.DISARMED),
        (_area(AreaMode.TOTAL), AlarmControlPanelState.ARMED_AWAY),
        (_area(AreaMode.PARTIAL), AlarmControlPanelState.ARMED_HOME),
        (_area(AreaMode.SNAPSHOT), AlarmControlPanelState.ARMED_NIGHT),
        (
            _area(AreaMode.DISARMED, state=AreaState.ALARM),
            AlarmControlPanelState.TRIGGERED,
        ),
        (
            _area(AreaMode.TOTAL, state=AreaState.SABOTAGE),
            AlarmControlPanelState.TRIGGERED,
        ),
        (
            _area(AreaMode.TOTAL, alarm_memory=True),
            AlarmControlPanelState.TRIGGERED,
        ),
        # alarm_memory while disarmed is NOT triggered (mode not armed).
        (
            _area(AreaMode.DISARMED, alarm_memory=True),
            AlarmControlPanelState.DISARMED,
        ),
    ],
)
def test_state_mapping(entity_factory, area: Area, expected) -> None:
    """The area mode/state maps to the right alarm panel state."""
    entity = entity_factory(area)
    assert entity.alarm_state == expected


def test_static_properties(entity_factory) -> None:
    """Features, code requirement, unique_id and device info are correct."""
    entity = entity_factory(_area(AreaMode.DISARMED))
    assert entity.code_arm_required is False
    assert entity.code_format is None
    assert entity.supported_features == (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )
    entry_id = entity.coordinator.config_entry.entry_id
    assert entity.unique_id == f"{entry_id}_area_1"
    assert entity.name == "Home"
    assert entity.available is True
    assert entity.device_info["manufacturer"] == "INIM"
    assert entity.device_info["model"] == "PX500"
    assert entity.device_info["sw_version"] == "4.07"


def test_extra_state_attributes(entity_factory) -> None:
    """Extra attributes expose area_id, alarm_memory and raw names."""
    entity = entity_factory(_area(AreaMode.TOTAL, state=AreaState.SABOTAGE, alarm_memory=True))
    attrs = entity.extra_state_attributes
    assert attrs == {
        "area_id": 1,
        "alarm_memory": True,
        "mode": "TOTAL",
        "state": "SABOTAGE",
    }


def test_area_missing_from_the_data(entity_factory) -> None:
    """An entity whose area is gone is unavailable and reports nothing."""
    entity = entity_factory(_area(AreaMode.DISARMED), entity_area_id=9)
    assert entity.available is False
    assert entity.alarm_state is None
    assert entity.name is None
    assert entity.extra_state_attributes == {"area_id": 9}


async def test_disarm_sends_native_and_goes_optimistic(entity_factory) -> None:
    """Disarm sends op 3 (no zone check), re-reads the panel, goes optimistic."""
    entity = entity_factory(_area(AreaMode.TOTAL))
    client = entity.coordinator.client

    await entity.async_alarm_disarm()

    client.async_set_area_modes.assert_awaited_once_with(
        {1: AreaMode.DISARMED}, require_ready=set()
    )
    entity.coordinator.async_refresh.assert_awaited_once()
    assert entity.alarm_state == AlarmControlPanelState.DISARMED


@pytest.mark.parametrize(
    ("method", "mode", "expected"),
    [
        ("async_alarm_arm_away", AreaMode.TOTAL, AlarmControlPanelState.ARMED_AWAY),
        ("async_alarm_arm_home", AreaMode.PARTIAL, AlarmControlPanelState.ARMED_HOME),
        ("async_alarm_arm_night", AreaMode.SNAPSHOT, AlarmControlPanelState.ARMED_NIGHT),
    ],
)
async def test_arm_sends_native_and_goes_optimistic(
    entity_factory, method: str, mode: AreaMode, expected
) -> None:
    """Each arm method sends the area's mode, checking the area's zones first."""
    entity = entity_factory(_area(AreaMode.DISARMED))
    client = entity.coordinator.client

    await getattr(entity, method)()

    # Zone 1 (not interior, not auto-bypass) is in area 1: it must be ready.
    client.async_set_area_modes.assert_awaited_once_with({1: mode}, require_ready={1})
    entity.coordinator.async_refresh.assert_awaited_once()
    assert entity.alarm_state == expected


async def test_arm_with_a_zone_not_ready_raises(entity_factory) -> None:
    """Zones not ready: nothing is sent, the state stays as it is."""
    entity = entity_factory(_area(AreaMode.DISARMED))
    entity.coordinator.client.async_set_area_modes.side_effect = NativeZonesNotReady(3, "open")

    with pytest.raises(HomeAssistantError) as exc_info:
        await entity.async_alarm_arm_away()

    assert exc_info.value.translation_key == "zones_not_ready"
    assert entity.alarm_state == AlarmControlPanelState.DISARMED
    entity.coordinator.async_refresh.assert_not_awaited()


async def test_arm_not_sent_raises_command_failed(entity_factory) -> None:
    """A command that never left raises command_failed, with no optimistic state."""
    entity = entity_factory(_area(AreaMode.DISARMED))
    entity.coordinator.client.async_set_area_modes.side_effect = NativeCommandNotSent(
        3, "connection refused"
    )

    with pytest.raises(HomeAssistantError) as exc_info:
        await entity.async_alarm_arm_home()

    assert exc_info.value.translation_key == "command_failed"
    assert entity.alarm_state == AlarmControlPanelState.DISARMED


@pytest.mark.parametrize("label", ["AREA       006 ", "Box", "Area 5"])
def test_area_entity_enabled_by_default(entity_factory, label: str) -> None:
    """Whatever the label: the area selection, not the name, decides if it exists."""
    entity = entity_factory(_area(AreaMode.DISARMED, label=label))
    assert entity.entity_registry_enabled_default is True


def test_optimistic_state_cleared_on_update(entity_factory) -> None:
    """The optimistic state is cleared once fresh coordinator data arrives."""
    entity = entity_factory(_area(AreaMode.DISARMED))
    entity._optimistic_state = AlarmControlPanelState.ARMED_AWAY
    # Simulate a coordinator update with disarmed data.
    entity._handle_coordinator_update()
    assert entity.alarm_state == AlarmControlPanelState.DISARMED


async def test_setup_follows_the_live_area_status(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    area_statuses: dict[int, NativeAreaStatus],
) -> None:
    """End to end: the area's state follows the native partition status."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity_id = er.async_get(hass).async_get_entity_id(
        "alarm_control_panel", DOMAIN, f"{mock_config_entry.entry_id}_area_1"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == AlarmControlPanelState.DISARMED
    coordinator = mock_config_entry.runtime_data.coordinator

    area_statuses[1] = NativeAreaStatus(mode=AreaMode.TOTAL, alarm=False, alarm_memory=False)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == AlarmControlPanelState.ARMED_AWAY

    area_statuses[1] = NativeAreaStatus(mode=AreaMode.TOTAL, alarm=True, alarm_memory=True)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == AlarmControlPanelState.TRIGGERED


@pytest.fixture
def code_entity_factory(
    hass: HomeAssistant,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
):
    """Build an alarm entity whose entry has the require-code option on."""

    def _factory(area: Area) -> InimAlarmControlPanel:
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="INIM Prime",
            data={"host": "192.0.2.10", CONF_LOCAL_PASSWORD: "pass"},
            options={CONF_REQUIRE_CODE: True},
            unique_id="192.0.2.10",
            version=2,
        )
        entry.add_to_hass(hass)
        coordinator = InimDataUpdateCoordinator(
            hass, entry, mock_local_client, mock_local_config
        )
        coordinator.data = InimData(
            version=Version(raw="4.07 PX500"),
            areas=[area],
            zones=[],
            scenarios=[],
            outputs=[],
            fault=Fault(vcc=13.7, has_faults=False),
            power=Power(battery_voltage=13.4),
            panel=PanelStatus(service=False, programming=False),
        )
        coordinator.async_refresh = AsyncMock()
        entity = InimAlarmControlPanel(coordinator, area.id)
        entity.hass = hass
        entity.entity_id = "alarm_control_panel.home"
        return entity

    return _factory


def test_require_code_static_properties(code_entity_factory) -> None:
    """With the option on, the entity requires a numeric code to arm and disarm."""
    entity = code_entity_factory(_area(AreaMode.DISARMED))
    assert entity.code_arm_required is True
    assert entity.code_format is CodeFormat.NUMBER


async def test_arm_with_valid_code_verifies_then_sends(code_entity_factory) -> None:
    """A code the panel accepts (op 0x04) is verified, then the arm is sent."""
    entity = code_entity_factory(_area(AreaMode.DISARMED))
    client = entity.coordinator.client
    client.async_check_code.return_value = NativeCodeCheck(
        valid=True, code_type=1, raw=b"\x01"
    )

    await entity.async_alarm_arm_away("123456")

    client.async_check_code.assert_awaited_once_with("123456")
    client.async_set_area_modes.assert_awaited_once_with({1: AreaMode.TOTAL}, require_ready={1})
    assert entity.alarm_state == AlarmControlPanelState.ARMED_AWAY


async def test_disarm_with_valid_code_verifies_then_sends(code_entity_factory) -> None:
    """Disarm also goes through the panel code check first."""
    entity = code_entity_factory(_area(AreaMode.TOTAL))
    client = entity.coordinator.client
    client.async_check_code.return_value = NativeCodeCheck(
        valid=True, code_type=1, raw=b"\x01"
    )

    await entity.async_alarm_disarm("123456")

    client.async_check_code.assert_awaited_once_with("123456")
    client.async_set_area_modes.assert_awaited_once_with({1: AreaMode.DISARMED}, require_ready=set())
    assert entity.alarm_state == AlarmControlPanelState.DISARMED


async def test_arm_with_invalid_code_raises_and_sends_nothing(code_entity_factory) -> None:
    """A code the panel rejects raises and no arm command is sent."""
    entity = code_entity_factory(_area(AreaMode.DISARMED))
    client = entity.coordinator.client
    client.async_check_code.return_value = NativeCodeCheck(
        valid=False, code_type=0, raw=b"\x00"
    )

    with pytest.raises(ServiceValidationError):
        await entity.async_alarm_arm_away("000000")

    client.async_check_code.assert_awaited_once_with("000000")
    client.async_set_area_modes.assert_not_awaited()


async def test_arm_without_code_raises_before_contacting_the_panel(code_entity_factory) -> None:
    """A missing code is rejected without even a panel code check."""
    entity = code_entity_factory(_area(AreaMode.DISARMED))
    client = entity.coordinator.client

    with pytest.raises(ServiceValidationError):
        await entity.async_alarm_arm_away(None)

    client.async_check_code.assert_not_awaited()
    client.async_set_area_modes.assert_not_awaited()


async def test_code_check_connection_failure_raises_and_sends_nothing(
    code_entity_factory,
) -> None:
    """If the panel cannot verify the code, nothing is armed (fail-closed)."""
    entity = code_entity_factory(_area(AreaMode.DISARMED))
    client = entity.coordinator.client
    client.async_check_code.side_effect = Local6004Error("no connection")

    with pytest.raises(HomeAssistantError):
        await entity.async_alarm_arm_away("123456")

    client.async_set_area_modes.assert_not_awaited()


async def test_arm_without_require_code_never_checks_code(entity_factory) -> None:
    """With the option off (default), no code check is made before arming."""
    entity = entity_factory(_area(AreaMode.DISARMED))
    client = entity.coordinator.client

    await entity.async_alarm_arm_away()

    client.async_check_code.assert_not_awaited()
    client.async_set_area_modes.assert_awaited_once()
