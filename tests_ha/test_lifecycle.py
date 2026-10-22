"""Dynamic entity-lifecycle tests for the INIM Prime integration.

These drive the *real* integration end-to-end through ``hass`` (full
``async_setup_entry`` + the per-platform coordinator-listener sync). The
objects come from the native structure read at setup; the tests swap the
running coordinator's structure to drive the platforms' listeners and assert:

* a NEW object id appearing in coordinator data adds exactly one entity on the
  next update (and not again on subsequent updates);
* a panel-side label change is reflected by the entity's friendly name;
* a removed object id makes its entity ``unavailable`` (not deleted);
* a *dynamically-added* factory-default area gets no entities (the default
  area selection), and a dynamically-added output switch keeps the declutter
  gating;
* no duplicate entities appear after several coordinator updates.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import NativeObject
from custom_components.inim_prime.coordinator import InimDataUpdateCoordinator


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> InimDataUpdateCoordinator:
    """Set up the integration; return its coordinator."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data.coordinator


async def _restructure(
    hass: HomeAssistant, coordinator: InimDataUpdateCoordinator, **kinds
) -> None:
    """Replace some structure kinds, refresh and let the listeners run."""
    coordinator.structure = replace(coordinator.structure, **kinds)
    await coordinator.async_refresh()
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
) -> None:
    """A new area id adds one alarm_control_panel on the next update only."""
    coordinator = await _setup(hass, mock_config_entry)
    new_uid = f"{mock_config_entry.entry_id}_area_2"
    assert _count(hass, mock_config_entry, new_uid) == 0

    await _restructure(
        hass, coordinator, areas=[*coordinator.structure.areas, NativeObject(2, "Garage")]
    )
    assert _count(hass, mock_config_entry, new_uid) == 1

    # Several more identical updates add nothing further (no duplicates).
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _count(hass, mock_config_entry, new_uid) == 1
    assert _count(hass, mock_config_entry, f"{mock_config_entry.entry_id}_area_1") == 1


async def test_label_change_reflected_in_name(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
) -> None:
    """Renaming an area on the panel updates the entity's friendly name."""
    coordinator = await _setup(hass, mock_config_entry)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "alarm_control_panel", "inim_prime", f"{mock_config_entry.entry_id}_area_1"
    )
    assert entity_id is not None
    # has_entity_name prefixes the device name; assert on the label portion.
    assert hass.states.get(entity_id).name.endswith("Home")

    await _restructure(hass, coordinator, areas=[NativeObject(1, "Main Floor")])

    assert hass.states.get(entity_id).name.endswith("Main Floor")


async def test_removed_zone_becomes_unavailable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
) -> None:
    """A zone id disappearing makes its binary_sensor unavailable, not deleted."""
    coordinator = await _setup(hass, mock_config_entry)
    registry = er.async_get(hass)
    uid = f"{mock_config_entry.entry_id}_zone_1"
    entity_id = registry.async_get_entity_id("binary_sensor", "inim_prime", uid)
    assert entity_id is not None
    assert hass.states.get(entity_id).state != "unavailable"

    await _restructure(hass, coordinator, zones=[])

    assert hass.states.get(entity_id).state == "unavailable"
    assert _count(hass, mock_config_entry, uid) == 1


async def test_dynamic_factory_default_area_not_created(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
) -> None:
    """A dynamically-added factory-default area gets no entities by default."""
    coordinator = await _setup(hass, mock_config_entry)

    await _restructure(
        hass,
        coordinator,
        areas=[*coordinator.structure.areas, NativeObject(6, "AREA       006 ")],
    )

    for uid in ("area_6", "area_memory_6", "button_6"):
        assert _count(hass, mock_config_entry, f"{mock_config_entry.entry_id}_{uid}") == 0
    # The area itself stays in the data, for commands and scenarios.
    assert 6 in {area.id for area in coordinator.data.areas}


async def test_dynamic_output_switch_disabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
) -> None:
    """A dynamically-added output switch keeps the declutter (disabled) gating."""
    coordinator = await _setup(hass, mock_config_entry)

    await _restructure(
        hass, coordinator, outputs=[*coordinator.structure.outputs, NativeObject(2, "Gate")]
    )

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "switch", "inim_prime", f"{mock_config_entry.entry_id}_output_2"
    )
    assert entity_id is not None
    assert registry.entities[entity_id].disabled_by is not None


async def test_new_scenario_adds_apply_button_without_reload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
) -> None:
    """A new scenario gets an 'apply scenario' button without a reload."""
    coordinator = await _setup(hass, mock_config_entry)
    registry = er.async_get(hass)
    new_uid = f"{mock_config_entry.entry_id}_scenario_apply_2"
    assert registry.async_get_entity_id("button", "inim_prime", new_uid) is None

    await _restructure(
        hass, coordinator, scenarios=[*coordinator.structure.scenarios, NativeObject(2, "Night")]
    )

    assert registry.async_get_entity_id("button", "inim_prime", new_uid) is not None
