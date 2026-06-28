"""Dynamic entity-lifecycle tests for the INIM Prime integration.

These drive the *real* integration end-to-end through ``hass`` (full
``async_setup_entry`` + the per-platform coordinator-listener sync) and assert:

* a NEW object id appearing in coordinator data adds exactly one entity on the
  next update (and not again on subsequent updates);
* a panel-side label change is reflected by the entity's friendly name;
* a removed object id makes its entity ``unavailable`` (not deleted);
* the declutter gating still applies to a *dynamically-added* factory-default
  area and to a dynamically-added output switch;
* no duplicate entities appear after several coordinator updates.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import (
    Area,
    AreaMode,
    AreaState,
    Output,
    Scenario,
    Zone,
    ZoneState,
)


def _area(area_id: int, label: str) -> Area:
    return Area(
        id=area_id,
        label=label,
        mode=AreaMode.DISARMED,
        state=AreaState.READY,
        alarm_memory=False,
    )


def _zone(zone_id: int, label: str) -> Zone:
    return Zone(
        id=zone_id,
        label=label,
        terminal=zone_id,
        state=ZoneState.READY,
        alarm_memory=False,
        excluded=False,
    )


async def _setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
) -> tuple[MockConfigEntry, AsyncMock]:
    """Set up the integration; return (entry, mock client)."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry, patch_client


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Force a coordinator refresh and let listeners run."""
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


def _count(hass: HomeAssistant, entry: MockConfigEntry, unique_id: str) -> int:
    """Count registry entries with the given unique_id for this entry."""
    registry = er.async_get(hass)
    return sum(
        1
        for e in registry.entities.values()
        if e.config_entry_id == entry.entry_id and e.unique_id == unique_id
    )


async def test_new_area_adds_exactly_one_entity_and_not_again(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    sample_areas,
) -> None:
    """A new area id adds one alarm_control_panel on the next update only."""
    entry, client = await _setup(hass, mock_config_entry, patch_client)
    registry = er.async_get(hass)

    new_uid = f"{entry.entry_id}_area_2"
    assert _count(hass, entry, new_uid) == 0

    # Panel now reports a second area.
    client.get_areas.return_value = [*sample_areas, _area(2, "Garage")]
    await _refresh(hass, entry)
    assert _count(hass, entry, new_uid) == 1

    # Several more identical updates add nothing further (no duplicates).
    await _refresh(hass, entry)
    await _refresh(hass, entry)
    assert _count(hass, entry, new_uid) == 1

    # The original area is still a single entity too.
    assert _count(hass, entry, f"{entry.entry_id}_area_1") == 1


async def test_label_change_reflected_in_name(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
) -> None:
    """Renaming an area on the panel updates the entity's friendly name."""
    entry, client = await _setup(hass, mock_config_entry, patch_client)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "alarm_control_panel", "inim_prime", f"{entry.entry_id}_area_1"
    )
    assert entity_id is not None
    # has_entity_name prefixes the device name; assert on the label portion.
    assert hass.states.get(entity_id).name.endswith("Home")

    client.get_areas.return_value = [_area(1, "Living Room")]
    await _refresh(hass, entry)

    assert hass.states.get(entity_id).name.endswith("Living Room")


async def test_removed_zone_becomes_unavailable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
) -> None:
    """A zone id disappearing makes its binary_sensor unavailable, not deleted."""
    entry, client = await _setup(hass, mock_config_entry, patch_client)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "binary_sensor", "inim_prime", f"{entry.entry_id}_zone_1"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state != "unavailable"

    client.get_zones.return_value = []
    await _refresh(hass, entry)

    assert hass.states.get(entity_id).state == "unavailable"
    # Still registered (not deleted from the registry).
    assert _count(hass, entry, f"{entry.entry_id}_zone_1") == 1


async def test_dynamic_factory_default_area_disabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    sample_areas,
) -> None:
    """A dynamically-added factory-default area is disabled by default."""
    entry, client = await _setup(hass, mock_config_entry, patch_client)

    client.get_areas.return_value = [*sample_areas, _area(6, "AREA       006 ")]
    await _refresh(hass, entry)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "alarm_control_panel", "inim_prime", f"{entry.entry_id}_area_6"
    )
    assert entity_id is not None
    assert registry.entities[entity_id].disabled_by is not None


async def test_dynamic_output_switch_disabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    sample_outputs,
) -> None:
    """A dynamically-added output switch keeps the declutter (disabled) gating."""
    entry, client = await _setup(hass, mock_config_entry, patch_client)

    client.get_outputs.return_value = [
        *sample_outputs,
        Output(id=2, label="Gate", terminal=2, state=0, type=0),
    ]
    await _refresh(hass, entry)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("switch", "inim_prime", f"{entry.entry_id}_output_2")
    assert entity_id is not None
    assert registry.entities[entity_id].disabled_by is not None


async def test_new_scenario_adds_apply_button_without_reload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    sample_scenarios,
) -> None:
    """A new scenario gets an 'apply scenario' button without a reload."""
    entry, client = await _setup(hass, mock_config_entry, patch_client)
    registry = er.async_get(hass)
    new_uid = f"{entry.entry_id}_scenario_apply_2"
    assert registry.async_get_entity_id("button", "inim_prime", new_uid) is None

    client.get_scenarios.return_value = [
        *sample_scenarios,
        Scenario(id=2, label="Night", active=False),
    ]
    await _refresh(hass, entry)

    assert registry.async_get_entity_id("button", "inim_prime", new_uid) is not None


async def test_new_scenario_adds_one_binary_sensor(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    sample_scenarios,
) -> None:
    """A new scenario adds exactly one per-scenario binary_sensor, once."""
    entry, client = await _setup(hass, mock_config_entry, patch_client)
    new_uid = f"{entry.entry_id}_scenario_active_2"
    assert _count(hass, entry, new_uid) == 0

    client.get_scenarios.return_value = [
        *sample_scenarios,
        Scenario(id=2, label="Night", active=False),
    ]
    await _refresh(hass, entry)
    await _refresh(hass, entry)
    assert _count(hass, entry, new_uid) == 1
