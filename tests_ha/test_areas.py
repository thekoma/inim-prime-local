"""Tests for the "areas to show" selection (areas.py).

These drive the real integration through ``hass``: the selection decides which
areas get entities (alarm panel, alarm-memory sensor, clear-memory button), the
registry is brought in line with it at setup, and a hidden area stays in the
coordinator data for commands and scenarios.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import Local6004Config, NativeObject
from custom_components.inim_prime.const import CONF_AREAS, CONF_AREAS_OFFERED, DOMAIN

# Per-area unique-id stems, by entity domain.
_AREA_ENTITIES = {
    "alarm_control_panel": "area_{}",
    "binary_sensor": "area_memory_{}",
    "button": "button_{}",
}


async def _open_area_settings(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    """Open the options menu and step into the settings form (areas live there)."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )


# Three named areas (ids 0, 1, 4) and one factory-default one (id 5).
_AREAS = [
    NativeObject(0, "Day"),
    NativeObject(1, "Night"),
    NativeObject(4, "Entrance"),
    NativeObject(5, "AREA       006 "),
]


@pytest.fixture
def mock_local_config(mock_local_config: Local6004Config) -> Local6004Config:
    """The panel's areas, as the native structure read lists them."""
    structure = mock_local_config.structure
    assert structure is not None
    return replace(mock_local_config, structure=replace(structure, areas=list(_AREAS)))


def _entry(entry_data: dict, options: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="INIM Prime",
        data=entry_data,
        options=options or {},
        unique_id="192.0.2.10",
        version=2,
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _registered(hass: HomeAssistant, entry: MockConfigEntry, area_id: int) -> dict[str, bool]:
    """Return, per domain, whether this area's entity is in the registry."""
    registry = er.async_get(hass)
    return {
        domain: registry.async_get_entity_id(
            domain, DOMAIN, f"{entry.entry_id}_{stem.format(area_id)}"
        )
        is not None
        for domain, stem in _AREA_ENTITIES.items()
    }


def _preregister(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    area_id: int,
    disabled_by: er.RegistryEntryDisabler | None = None,
) -> None:
    """Register this area's three entities as an earlier version would have."""
    registry = er.async_get(hass)
    for domain, stem in _AREA_ENTITIES.items():
        registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{entry.entry_id}_{stem.format(area_id)}",
            config_entry=entry,
            disabled_by=disabled_by,
        )


def _all(value: bool) -> dict[str, bool]:
    return dict.fromkeys(_AREA_ENTITIES, value)


async def test_default_shows_real_names_only(
    hass: HomeAssistant, entry_data: dict, patch_client: AsyncMock
) -> None:
    """Without a saved selection, every area with a real name gets entities."""
    entry = _entry(entry_data)
    await _setup(hass, entry)

    for area_id in (0, 1, 4):
        assert _registered(hass, entry, area_id) == _all(True)
    assert _registered(hass, entry, 5) == _all(False)


async def test_selection_creates_only_selected_areas(
    hass: HomeAssistant, entry_data: dict, patch_client: AsyncMock
) -> None:
    """A saved selection shows exactly the selected ids; the data keeps them all."""
    entry = _entry(entry_data, {CONF_AREAS: ["0", "4"], CONF_AREAS_OFFERED: ["0", "1", "4", "5"]})
    await _setup(hass, entry)

    assert _registered(hass, entry, 0) == _all(True)
    assert _registered(hass, entry, 4) == _all(True)
    assert _registered(hass, entry, 1) == _all(False)
    assert _registered(hass, entry, 5) == _all(False)
    # Hidden areas stay in the data, so commands and scenarios still see them.
    assert {area.id for area in entry.runtime_data.coordinator.data.areas} == {0, 1, 4, 5}


async def test_selected_factory_named_area_is_shown(
    hass: HomeAssistant, entry_data: dict, patch_client: AsyncMock
) -> None:
    """An explicit selection wins over the factory name."""
    entry = _entry(entry_data, {CONF_AREAS: ["5"], CONF_AREAS_OFFERED: ["0", "1", "4", "5"]})
    await _setup(hass, entry)

    assert _registered(hass, entry, 5) == _all(True)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "alarm_control_panel", DOMAIN, f"{entry.entry_id}_area_5"
    )
    assert registry.entities[entity_id].disabled_by is None


async def test_hidden_area_entities_are_removed(
    hass: HomeAssistant, entry_data: dict, patch_client: AsyncMock
) -> None:
    """Entities left by an earlier version for a hidden area are removed at setup."""
    entry = _entry(entry_data, {CONF_AREAS: ["0"], CONF_AREAS_OFFERED: ["0", "1", "4", "5"]})
    entry.add_to_hass(hass)
    _preregister(hass, entry, 1)
    _preregister(hass, entry, 5, er.RegistryEntryDisabler.INTEGRATION)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert _registered(hass, entry, 1) == _all(False)
    assert _registered(hass, entry, 5) == _all(False)
    assert _registered(hass, entry, 0) == _all(True)
    # Other entities (a zone sensor) are untouched.
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("binary_sensor", DOMAIN, f"{entry.entry_id}_zone_1")


async def test_shown_area_disabled_by_the_old_rule_is_enabled_again(
    hass: HomeAssistant, entry_data: dict, patch_client: AsyncMock
) -> None:
    """An integration-disabled entity of a shown area comes back; a user-disabled one does not."""
    entry = _entry(entry_data)
    entry.add_to_hass(hass)
    # Area 4 had a factory name when first added (so the old rule disabled it)
    # and has a real one now; the owner disabled area 1 by hand.
    _preregister(hass, entry, 4, er.RegistryEntryDisabler.INTEGRATION)
    _preregister(hass, entry, 1, er.RegistryEntryDisabler.USER)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    for domain, stem in _AREA_ENTITIES.items():
        shown = registry.async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{stem.format(4)}")
        manual = registry.async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{stem.format(1)}")
        assert registry.entities[shown].disabled_by is None
        assert registry.entities[manual].disabled_by is er.RegistryEntryDisabler.USER


async def test_default_keeps_a_factory_named_area_enabled_by_hand(
    hass: HomeAssistant, entry_data: dict, patch_client: AsyncMock
) -> None:
    """An upgrade keeps a factory-named area whose alarm panel the owner had enabled."""
    entry = _entry(entry_data)
    entry.add_to_hass(hass)
    # The old rule created all three disabled; the owner enabled the alarm panel.
    _preregister(hass, entry, 5, er.RegistryEntryDisabler.INTEGRATION)
    registry = er.async_get(hass)
    alarm = registry.async_get_entity_id("alarm_control_panel", DOMAIN, f"{entry.entry_id}_area_5")
    registry.async_update_entity(alarm, disabled_by=None)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert _registered(hass, entry, 5) == _all(True)
    for domain, stem in _AREA_ENTITIES.items():
        entity_id = registry.async_get_entity_id(
            domain, DOMAIN, f"{entry.entry_id}_{stem.format(5)}"
        )
        assert registry.entities[entity_id].disabled_by is None
    assert hass.states.get(alarm) is not None
    # And the options form preselects it with the named areas.
    result = await _open_area_settings(hass, entry)
    key = next(k for k in result["data_schema"].schema if k == CONF_AREAS)
    assert key.default() == ["0", "1", "4", "5"]


async def test_default_leaves_unknown_area_entities_alone(
    hass: HomeAssistant, entry_data: dict, patch_client: AsyncMock
) -> None:
    """Without a selection, an area missing from the data keeps its entities."""
    entry = _entry(entry_data)
    entry.add_to_hass(hass)
    _preregister(hass, entry, 9)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert _registered(hass, entry, 9) == _all(True)


async def test_area_added_after_the_selection_is_hidden_and_warned_once(
    hass: HomeAssistant,
    entry_data: dict,
    patch_client: AsyncMock,
    mock_local_config: Local6004Config,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A new panel area stays hidden, with one warning; a deselected one is not warned."""
    entry = _entry(entry_data, {CONF_AREAS: ["0"], CONF_AREAS_OFFERED: ["0", "1", "4", "5"]})
    caplog.set_level(logging.WARNING)
    await _setup(hass, entry)
    assert "is not shown" not in caplog.text
    assert await hass.config_entries.async_unload(entry.entry_id)

    # Area 7 was added on the panel since: the structure read at setup lists it.
    structure = mock_local_config.structure
    assert structure is not None
    patch_client.async_read_config.return_value = replace(
        mock_local_config,
        structure=replace(structure, areas=[*_AREAS, NativeObject(7, "Garage")]),
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for _ in range(3):
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()

    assert _registered(hass, entry, 7) == _all(False)
    warnings = [r for r in caplog.records if "is not shown" in r.getMessage()]
    assert len(warnings) == 1
    assert "Garage (number 8)" in warnings[0].getMessage()


async def test_options_flow_offers_areas_and_saves_the_selection(
    hass: HomeAssistant, entry_data: dict, patch_client: AsyncMock
) -> None:
    """The form lists every area (real names preselected) and saves ids plus the offer."""
    entry = _entry(entry_data)
    await _setup(hass, entry)

    result = await _open_area_settings(hass, entry)
    assert result["type"] is FlowResultType.FORM
    key = next(k for k in result["data_schema"].schema if k == CONF_AREAS)
    assert key.default() == ["0", "1", "4"]
    selector = result["data_schema"].schema[key]
    assert [(o["value"], o["label"]) for o in selector.config["options"]] == [
        ("0", "Day"),
        ("1", "Night"),
        ("4", "Entrance"),
        ("5", "AREA 006"),
    ]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_AREAS: ["4", "0"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options[CONF_AREAS] == ["0", "4"]
    assert entry.options[CONF_AREAS_OFFERED] == ["0", "1", "4", "5"]
    # The reload applied it: area 1 lost its entities, 0 and 4 kept theirs.
    assert _registered(hass, entry, 1) == _all(False)
    assert _registered(hass, entry, 0) == _all(True)
    assert _registered(hass, entry, 4) == _all(True)


async def test_options_flow_without_a_loaded_entry_has_no_area_field(
    hass: HomeAssistant, entry_data: dict, patch_client: AsyncMock
) -> None:
    """The area list comes from the running coordinator: no entry, no field."""
    entry = _entry(entry_data)
    entry.add_to_hass(hass)

    result = await _open_area_settings(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert CONF_AREAS not in result["data_schema"].schema
