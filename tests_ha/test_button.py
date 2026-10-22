"""Tests for the INIM Prime button platform."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.button import (
    InimApplyScenarioButton,
    InimClearAlarmMemoryButton,
    async_setup_entry,
)
from custom_components.inim_prime.client import (
    Area,
    AreaMode,
    AreaState,
    Fault,
    Local6004Config,
    Local6004Structure,
    NativeCommandNotSent,
    NativeZonesNotReady,
    Scenario,
    SceneDef,
    Version,
)
from custom_components.inim_prime.const import DOMAIN
from custom_components.inim_prime.coordinator import InimData

_VERSION = Version(raw="4.07 PX500", service=False)


def _make_coordinator(areas: list[Area], scenarios: list[Scenario] | None = None) -> MagicMock:
    """Build a fake coordinator carrying sample InimData and a native client.

    Scenario 1 arms area 1 away; scenario 2 arms area 2 away.
    """
    coordinator = MagicMock()
    coordinator.client = AsyncMock()
    coordinator.client.supports_native_writes = True
    coordinator.async_refresh = AsyncMock()
    coordinator.local_config = Local6004Config(
        firmware="4.07 PX500",
        layout_ok=True,
        scenes=[SceneDef(id=1, arms={1: "away"}), SceneDef(id=2, arms={2: "away"})],
    )
    coordinator.structure = Local6004Structure()
    coordinator.force_arm_on_open = False
    coordinator.config_entry = MagicMock(entry_id="abc123", title="INIM Prime")
    coordinator.data = InimData(
        version=_VERSION,
        areas=areas,
        zones=[],
        scenarios=scenarios or [],
        outputs=[],
        fault=Fault(vcc=13.7, has_faults=False),
    )
    return coordinator


@pytest.fixture
def two_areas() -> list[Area]:
    return [
        Area(
            id=1,
            label="Home",
            mode=AreaMode.DISARMED,
            state=AreaState.READY,
            alarm_memory=True,
        ),
        Area(
            id=2,
            label="Garage",
            mode=AreaMode.DISARMED,
            state=AreaState.READY,
            alarm_memory=False,
        ),
    ]


async def test_async_setup_entry_creates_one_button_per_area(two_areas) -> None:
    """One button entity is created per area."""
    coordinator = _make_coordinator(two_areas)
    entry = MagicMock()
    entry.options = {}
    entry.runtime_data.coordinator = coordinator

    added: list = []
    await async_setup_entry(MagicMock(), entry, lambda new, *a, **k: added.extend(list(new)))

    assert len(added) == 2
    assert all(isinstance(e, InimClearAlarmMemoryButton) for e in added)


def test_button_properties(two_areas) -> None:
    """The button exposes the expected identity and category."""
    coordinator = _make_coordinator(two_areas)
    button = InimClearAlarmMemoryButton(coordinator, two_areas[0])

    assert button.unique_id == "abc123_button_1"
    assert button.name == "Home clear alarm memory"
    assert button.entity_category is EntityCategory.CONFIG
    assert button.has_entity_name is True
    assert button.available is True

    device = button.device_info
    assert device["identifiers"] == {(DOMAIN, "abc123")}
    assert device["manufacturer"] == "INIM"
    assert device["model"] == "PX500"  # parsed model variant
    assert device["sw_version"] == "4.07"
    assert device["name"] == "INIM Prime"


def test_button_enabled_default_whatever_the_area_name() -> None:
    """Enabled whatever the label: the area selection decides if it exists."""
    areas = [
        Area(
            id=6,
            label="AREA       006 ",
            mode=AreaMode.DISARMED,
            state=AreaState.READY,
            alarm_memory=False,
        ),
        Area(
            id=7,
            label="Box",
            mode=AreaMode.DISARMED,
            state=AreaState.READY,
            alarm_memory=False,
        ),
    ]
    coordinator = _make_coordinator(areas)

    factory_button = InimClearAlarmMemoryButton(coordinator, areas[0])
    real_button = InimClearAlarmMemoryButton(coordinator, areas[1])

    assert factory_button.entity_registry_enabled_default is True
    assert real_button.entity_registry_enabled_default is True


async def test_async_press_resets_the_area_then_rereads(two_areas) -> None:
    """Pressing resets the area's alarm memory natively (op 16), then re-reads."""
    coordinator = _make_coordinator(two_areas)
    button = InimClearAlarmMemoryButton(coordinator, two_areas[1])

    await button.async_press()

    coordinator.client.async_reset_areas.assert_awaited_once_with([2])
    coordinator.async_refresh.assert_awaited_once()


async def test_clear_alarm_memory_not_sent(two_areas) -> None:
    """A reset that never left raises command_failed."""
    coordinator = _make_coordinator(two_areas)
    coordinator.client.async_reset_areas.side_effect = NativeCommandNotSent(16, "refused")
    button = InimClearAlarmMemoryButton(coordinator, two_areas[0])

    with pytest.raises(HomeAssistantError) as exc:
        await button.async_press()
    assert exc.value.translation_key == "command_failed"


def test_buttons_of_missing_objects(two_areas) -> None:
    """A button whose area or scenario is gone is unavailable, keeps its last name."""
    coordinator = _make_coordinator(two_areas, _scenarios())
    clear = InimClearAlarmMemoryButton(coordinator, two_areas[0])
    apply = InimApplyScenarioButton(coordinator, _scenarios()[0])
    coordinator.data = InimData(
        version=_VERSION,
        areas=[],
        zones=[],
        scenarios=[],
        outputs=[],
        fault=Fault(vcc=13.7, has_faults=False),
    )
    assert clear.available is False
    assert clear.name == "Home clear alarm memory"
    assert apply.available is False
    assert apply.name == "Away"


# ---------------------------------------------------------------------------
# Apply-scenario buttons (replaced the former active-scenario select)
# ---------------------------------------------------------------------------
def _scenarios() -> list[Scenario]:
    return [
        Scenario(id=1, label="Away", active=False),
        Scenario(id=2, label="Garage only", active=False),
    ]


async def test_setup_creates_apply_button_per_scenario(two_areas) -> None:
    """An apply-scenario button is created for each scenario."""
    coordinator = _make_coordinator(two_areas, _scenarios())
    entry = MagicMock()
    entry.options = {}
    entry.runtime_data.coordinator = coordinator

    added: list = []
    await async_setup_entry(MagicMock(), entry, lambda new, *a, **k: added.extend(list(new)))

    apply = [e for e in added if isinstance(e, InimApplyScenarioButton)]
    assert {e.unique_id for e in apply} == {
        "abc123_scenario_apply_1",
        "abc123_scenario_apply_2",
    }


def test_apply_scenario_button_properties(two_areas) -> None:
    """The apply button names itself after the live scenario label."""
    coordinator = _make_coordinator(two_areas, _scenarios())
    button = InimApplyScenarioButton(coordinator, _scenarios()[0])
    assert button.unique_id == "abc123_scenario_apply_1"
    assert button.name == "Away"
    assert button.has_entity_name is True
    assert button.available is True


async def test_apply_scenario_press_sends_its_modes(two_areas) -> None:
    """Pressing sends the scenario's area modes as one op 3, then re-reads."""
    coordinator = _make_coordinator(two_areas, _scenarios())
    button = InimApplyScenarioButton(coordinator, _scenarios()[1])

    await button.async_press()

    coordinator.client.async_set_area_modes.assert_awaited_once_with(
        {2: AreaMode.TOTAL}, require_ready=set()
    )
    coordinator.async_refresh.assert_awaited_once()


async def test_apply_scenario_force_arm_path(two_areas) -> None:
    """With the force-arm switch on, the button routes through async_force_arm."""
    coordinator = _make_coordinator(two_areas, _scenarios())
    coordinator.force_arm_on_open = True
    coordinator.hass = MagicMock()
    button = InimApplyScenarioButton(coordinator, _scenarios()[1])

    with patch("custom_components.inim_prime.button.async_force_arm", new=AsyncMock()) as force:
        await button.async_press()

    force.assert_awaited_once()
    assert force.await_args.kwargs == {"kind": "scenario", "obj_id": 2}
    # the plain apply path is NOT used when forcing
    coordinator.client.async_set_area_modes.assert_not_awaited()


async def test_apply_scenario_zones_not_ready(two_areas) -> None:
    """Zones not ready maps to the dedicated message and skips the re-read."""
    coordinator = _make_coordinator(two_areas, _scenarios())
    coordinator.client.async_set_area_modes.side_effect = NativeZonesNotReady(3, "open")
    button = InimApplyScenarioButton(coordinator, _scenarios()[0])
    with pytest.raises(HomeAssistantError) as exc:
        await button.async_press()
    assert exc.value.translation_key == "zones_not_ready"
    coordinator.async_refresh.assert_not_awaited()


async def test_apply_scenario_not_sent_command_failed(two_areas) -> None:
    """A command that never left maps to command_failed."""
    coordinator = _make_coordinator(two_areas, _scenarios())
    coordinator.client.async_set_area_modes.side_effect = NativeCommandNotSent(3, "refused")
    button = InimApplyScenarioButton(coordinator, _scenarios()[0])
    with pytest.raises(HomeAssistantError) as exc:
        await button.async_press()
    assert exc.value.translation_key == "command_failed"


async def test_apply_scenario_without_a_definition_is_not_supported(two_areas) -> None:
    """A scenario with no native definition is refused before anything is sent."""
    coordinator = _make_coordinator(two_areas, [Scenario(id=3, label="Other", active=False)])
    button = InimApplyScenarioButton(coordinator, coordinator.data.scenarios[0])
    with pytest.raises(HomeAssistantError) as exc:
        await button.async_press()
    assert exc.value.translation_key == "scenario_not_supported"
    coordinator.client.async_set_area_modes.assert_not_awaited()


async def test_apply_scenario_end_to_end(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """End to end: pressing the scenario button sends its definition natively."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity_id = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{mock_config_entry.entry_id}_scenario_apply_1"
    )
    assert entity_id is not None
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)

    # The harness scenario 1 disarms area 1: no zone check.
    patch_client.async_set_area_modes.assert_awaited_once_with(
        {1: AreaMode.DISARMED}, require_ready=set()
    )
