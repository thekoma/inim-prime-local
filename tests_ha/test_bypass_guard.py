"""A "Non escludibile" zone gets no bypass switch (switch.py)."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import Local6004Config
from custom_components.inim_prime.const import DOMAIN


@pytest.fixture
def mock_local_config(mock_local_config: Local6004Config) -> Local6004Config:
    """The sample zone is programmed "Non escludibile"."""
    structure = mock_local_config.structure
    assert structure is not None and structure.zones is not None
    locked = replace(structure.zones[0], options=0x04)
    return replace(mock_local_config, structure=replace(structure, zones=[locked]))


async def test_unbypassable_zone_has_no_bypass_switch(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """No switch is created, and one left by an earlier version is removed."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    uid = f"{mock_config_entry.entry_id}_zone_bypass_1"
    registry.async_get_or_create("switch", DOMAIN, uid, config_entry=mock_config_entry)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get_entity_id("switch", DOMAIN, uid) is None
    # The zone sensor itself is still there.
    assert registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{mock_config_entry.entry_id}_zone_1"
    )
