"""Tests for the INIM Prime alarm_control_panel platform."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.alarm_control_panel import (
    InimAlarmControlPanel,
)
from custom_components.inim_prime.client import Area, AreaMode, AreaState, ArmMode
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


def _make_coordinator(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    areas: list[Area],
    sample_version,
    sample_zones,
    sample_scenarios,
    sample_outputs,
    sample_fault,
    sample_api_stats,
) -> InimDataUpdateCoordinator:
    mock_config_entry.add_to_hass(hass)
    coordinator = InimDataUpdateCoordinator(hass, mock_config_entry, mock_client)
    coordinator.data = InimData(
        version=sample_version,
        areas=areas,
        zones=sample_zones,
        scenarios=sample_scenarios,
        outputs=sample_outputs,
        fault=sample_fault,
        api_stats=sample_api_stats,
    )
    return coordinator


@pytest.fixture
def entity_factory(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    sample_version,
    sample_zones,
    sample_scenarios,
    sample_outputs,
    sample_fault,
    sample_api_stats,
):
    """Build an entity bound to a coordinator carrying the given areas."""

    def _factory(area: Area) -> InimAlarmControlPanel:
        coordinator = _make_coordinator(
            hass,
            mock_config_entry,
            mock_client,
            [area],
            sample_version,
            sample_zones,
            sample_scenarios,
            sample_outputs,
            sample_fault,
            sample_api_stats,
        )
        entity = InimAlarmControlPanel(coordinator, area.id)
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


async def test_disarm_calls_client_and_optimistic(entity_factory) -> None:
    """Disarm calls the client, sets optimistic state, requests refresh."""
    entity = entity_factory(_area(AreaMode.TOTAL))
    entity.coordinator.async_request_refresh = AsyncMock()

    await entity.async_alarm_disarm()

    entity.coordinator.client.disarm_area.assert_awaited_once_with(1)
    assert entity.alarm_state == AlarmControlPanelState.DISARMED
    entity.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.parametrize(
    ("method", "mode", "expected"),
    [
        ("async_alarm_arm_away", ArmMode.TOTAL, AlarmControlPanelState.ARMED_AWAY),
        ("async_alarm_arm_home", ArmMode.PARTIAL, AlarmControlPanelState.ARMED_HOME),
        ("async_alarm_arm_night", ArmMode.SNAPSHOT, AlarmControlPanelState.ARMED_NIGHT),
    ],
)
async def test_arm_calls_client_and_optimistic(
    entity_factory, method: str, mode: ArmMode, expected
) -> None:
    """Each arm method calls arm_area with the right mode and goes optimistic."""
    entity = entity_factory(_area(AreaMode.DISARMED))
    entity.coordinator.async_request_refresh = AsyncMock()

    await getattr(entity, method)()

    entity.coordinator.client.arm_area.assert_awaited_once_with(1, mode)
    assert entity.alarm_state == expected
    entity.coordinator.async_request_refresh.assert_awaited_once()


def test_factory_default_area_disabled_by_default(entity_factory) -> None:
    """An unused factory-default area ("AREA 006") is hidden by default."""
    entity = entity_factory(_area(AreaMode.DISARMED, label="AREA       006 "))
    assert entity.entity_registry_enabled_default is False


@pytest.mark.parametrize("label", ["Box", "Area 5"])
def test_real_area_enabled_by_default(entity_factory, label: str) -> None:
    """Used areas (real names and the manual "Area 5") stay enabled."""
    entity = entity_factory(_area(AreaMode.DISARMED, label=label))
    assert entity.entity_registry_enabled_default is True


def test_setup_creates_one_entity_per_area(entity_factory) -> None:
    """The optimistic state is cleared once fresh coordinator data arrives."""
    entity = entity_factory(_area(AreaMode.DISARMED))
    entity._optimistic_state = AlarmControlPanelState.ARMED_AWAY
    # Simulate a coordinator update with disarmed data.
    entity._handle_coordinator_update()
    assert entity.alarm_state == AlarmControlPanelState.DISARMED
