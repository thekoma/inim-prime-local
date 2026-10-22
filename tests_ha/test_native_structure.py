"""Tests for the native (TCP 6004) static structure driving the entity set."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import (
    AreaMode,
    AreaState,
    Local6004Structure,
    NativeObject,
    NativeZoneDef,
    ZoneState,
)

# Zone 1506 is half B of terminal 1 and output 1505 the first output terminal,
# as numbered on firmware 5.x.
STRUCTURE = Local6004Structure(
    areas=[NativeObject(1, "Home"), NativeObject(2, "Garage")],
    zones=[
        NativeZoneDef(1, "Door", 1, (1,)),
        NativeZoneDef(1506, "Door B", 1, (2,)),
    ],
    scenarios=[NativeObject(1, "Away"), NativeObject(3, "Night")],
    outputs=[NativeObject(1, "Siren"), NativeObject(1505, "Bell")],
)


async def test_setup_builds_entities_from_the_structure(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
) -> None:
    """The structure read at setup decides the objects, their names and reads."""
    patch_client.async_read_config.return_value = replace(
        patch_client.async_read_config.return_value, structure=STRUCTURE
    )
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    entry_id = mock_config_entry.entry_id
    for platform, uid in (
        ("switch", f"{entry_id}_output_1"),
        ("switch", f"{entry_id}_output_1505"),
        ("binary_sensor", f"{entry_id}_zone_1506"),
        ("alarm_control_panel", f"{entry_id}_area_2"),
        ("button", f"{entry_id}_scenario_apply_3"),
    ):
        assert registry.async_get_entity_id(platform, "inim_prime", uid), uid
    output = registry.async_get(
        registry.async_get_entity_id("switch", "inim_prime", f"{entry_id}_output_1505")
    )
    assert output is not None
    assert output.disabled_by is er.RegistryEntryDisabler.INTEGRATION

    data = mock_config_entry.runtime_data.coordinator.data
    assert [a.label for a in data.areas] == ["Home", "Garage"]
    # Area 2 has no live status in the fixtures: it starts disarmed and ready.
    assert (data.areas[1].mode, data.areas[1].state) == (AreaMode.DISARMED, AreaState.READY)
    door_b = data.zones[1]
    assert (door_b.label, door_b.terminal, door_b.state) == ("Door B", 1, ZoneState.READY)
    assert [s.label for s in data.scenarios] == ["Away", "Night"]
    assert [(o.id, o.label) for o in data.outputs] == [(1, "Siren"), (1505, "Bell")]

    patch_client.async_get_zone_statuses.assert_awaited_with({1, 1506})
    patch_client.async_get_output_states.assert_awaited_with({1, 1505})
    assert await hass.config_entries.async_unload(entry_id)
