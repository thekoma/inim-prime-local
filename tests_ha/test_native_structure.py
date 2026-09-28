"""Tests for the native (TCP 6004) static structure overlay."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import (
    AreaMode,
    AreaState,
    Local6004Config,
    Local6004Structure,
    NativeAreaStatus,
    NativeObject,
    NativeZoneDef,
    NativeZoneStatus,
    ZoneState,
)
from custom_components.inim_prime.const import CONF_NATIVE_AREA_POLL
from custom_components.inim_prime.coordinator import InimDataUpdateCoordinator

# The cgi fixtures (conftest) hold area 1 "Home", zone 1 "Front Door",
# scenario 1 "Away" and output 1 "Siren".
STRUCTURE = Local6004Structure(
    areas=[NativeObject(1, "Casa"), NativeObject(2, "Garage")],
    zones=[
        NativeZoneDef(1, "Porta", 1, (1,)),
        NativeZoneDef(1006, "Porta B", 1, (2,)),
    ],
    scenarios=[NativeObject(1, "Via"), NativeObject(3, "Notte")],
    outputs=[NativeObject(1, "Sirena"), NativeObject(1005, "Sirene")],
)


def _config(structure: Local6004Structure | None) -> Local6004Config:
    return Local6004Config(firmware="4.07 PX020", layout_ok=True, structure=structure)


async def _coordinator(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    structure: Local6004Structure | None = STRUCTURE,
) -> InimDataUpdateCoordinator:
    mock_config_entry.add_to_hass(hass)
    coordinator = InimDataUpdateCoordinator(hass, mock_config_entry, mock_client)
    coordinator.local_config = _config(structure)
    return coordinator


async def test_structure_overlays_labels_and_existence(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """Native labels win, cgi state is kept, native-only objects get defaults."""
    mock_client.get_areas.return_value = [
        replace(mock_client.get_areas.return_value[0], mode=AreaMode.TOTAL)
    ]
    mock_client.get_scenarios.return_value = [
        replace(mock_client.get_scenarios.return_value[0], active=True)
    ]
    coordinator = await _coordinator(hass, mock_config_entry, mock_client)

    data = await coordinator._async_update_data()

    assert [(a.id, a.label) for a in data.areas] == [(1, "Casa"), (2, "Garage")]
    assert data.areas[0].mode is AreaMode.TOTAL  # cgi state kept
    assert data.areas[1].mode is AreaMode.DISARMED
    assert data.areas[1].state is AreaState.READY
    assert not data.areas[1].alarm_memory

    assert [(z.id, z.label) for z in data.zones] == [(1, "Porta"), (1006, "Porta B")]
    assert data.zones[1].terminal == 1006  # the cgi's convention: own id
    assert data.zones[1].state is ZoneState.READY
    assert not data.zones[1].excluded

    assert [(s.id, s.label, s.active) for s in data.scenarios] == [
        (1, "Via", True),
        (3, "Notte", False),
    ]
    assert [(o.id, o.label, o.state) for o in data.outputs] == [
        (1, "Sirena", 0),
        (1005, "Sirene", None),
    ]


async def test_native_only_objects_use_last_native_reading(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """A native-only area/zone starts from the last native status, not defaults."""
    coordinator = await _coordinator(hass, mock_config_entry, mock_client)
    coordinator._native_areas = {
        2: NativeAreaStatus(mode=AreaMode.TOTAL, alarm=True, alarm_memory=True)
    }
    coordinator._native_zones = {
        1006: NativeZoneStatus(state=ZoneState.ALARM, excluded=True, alarm_memory=True)
    }

    data = await coordinator._async_update_data()

    garage = data.areas[1]
    assert (garage.mode, garage.state, garage.alarm_memory) == (
        AreaMode.TOTAL,
        AreaState.ALARM,
        True,
    )
    zone = data.zones[1]
    assert (zone.state, zone.excluded, zone.alarm_memory) == (ZoneState.ALARM, True, True)


async def test_without_structure_the_cgi_snapshot_is_used(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """No native structure (or no local config at all) keeps pure cgi behaviour."""
    coordinator = await _coordinator(hass, mock_config_entry, mock_client, structure=None)
    data = await coordinator._async_update_data()
    assert [a.label for a in data.areas] == ["Home"]
    assert [o.label for o in data.outputs] == ["Siren"]

    coordinator.local_config = None
    data = await coordinator._async_update_data()
    assert [z.label for z in data.zones] == ["Front Door"]


async def test_mismatch_keeps_cgi_only_objects_and_warns_once(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """cgi-only areas/zones/scenarios are kept, cgi-only outputs dropped; one warning per kind."""
    structure = Local6004Structure(areas=[NativeObject(2, "Garage")])
    coordinator = await _coordinator(hass, mock_config_entry, mock_client, structure)

    with caplog.at_level(logging.WARNING):
        data = await coordinator._async_update_data()
        await coordinator._async_update_data()

    assert [(a.id, a.label) for a in data.areas] == [(2, "Garage"), (1, "Home")]
    assert [z.label for z in data.zones] == ["Front Door"]
    assert [s.label for s in data.scenarios] == ["Away"]
    assert data.outputs == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    # areas, zones and scenarios differ; outputs are never compared
    assert len(warnings) == 3
    assert "for areas: only cgi [1], only native [2]" in warnings[0]


async def test_native_only_objects_need_the_native_poll(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """With the native poll off, nothing would keep a native-only area/zone live."""
    coordinator = await _coordinator(hass, mock_config_entry, mock_client)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_NATIVE_AREA_POLL: False}
    )

    data = await coordinator._async_update_data()

    assert [a.id for a in data.areas] == [1]
    assert [z.id for z in data.zones] == [1]
    # scenarios and outputs carry no live state the poll would provide
    assert [s.id for s in data.scenarios] == [1, 3]
    assert [o.id for o in data.outputs] == [1, 1005]


async def test_matching_structure_does_not_warn(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Identical cgi and native sets log nothing, even when outputs differ."""
    structure = Local6004Structure(
        areas=[NativeObject(1, "Home")],
        zones=[NativeZoneDef(1, "Front Door", 1, (1,))],
        scenarios=[NativeObject(1, "Away")],
        outputs=[NativeObject(1005, "Sirene")],
    )
    coordinator = await _coordinator(hass, mock_config_entry, mock_client, structure)
    with caplog.at_level(logging.WARNING):
        await coordinator._async_update_data()
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


async def test_overlapping_native_poll_tick_is_skipped(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """A tick that fires while a slow native read runs returns instead of queueing."""
    coordinator = await _coordinator(hass, mock_config_entry, mock_client, structure=None)
    coordinator.async_set_updated_data(await coordinator._async_update_data())
    release = asyncio.Event()

    async def slow_statuses() -> dict[int, NativeAreaStatus]:
        await release.wait()
        return {}

    native = AsyncMock()
    native.async_get_area_statuses.side_effect = slow_statuses
    native.async_get_zone_statuses.return_value = {}
    coordinator.async_attach_native(native)

    first = asyncio.create_task(coordinator.async_native_poll())
    await asyncio.sleep(0)
    await coordinator.async_native_poll()  # overlapping tick: skipped at once
    assert native.async_get_area_statuses.await_count == 1

    release.set()
    await first
    assert not coordinator._native_polling
    await coordinator.async_native_poll()  # the next tick reads again
    assert native.async_get_area_statuses.await_count == 2


async def test_setup_builds_entities_from_native_structure(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    mock_local_client: AsyncMock,
) -> None:
    """The first snapshot already carries the native structure (read before the cgi)."""
    mock_local_client.async_read_config.return_value = replace(
        mock_local_client.async_read_config.return_value, structure=STRUCTURE
    )
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    entry_id = mock_config_entry.entry_id
    for uid in (f"{entry_id}_output_1", f"{entry_id}_output_1005", f"{entry_id}_zone_1006"):
        assert registry.async_get_entity_id(
            "switch" if "output" in uid else "binary_sensor", "inim_prime", uid
        ), uid
    output = registry.async_get(
        registry.async_get_entity_id("switch", "inim_prime", f"{entry_id}_output_1005")
    )
    assert output is not None
    assert output.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert [a.label for a in mock_config_entry.runtime_data.coordinator.data.areas] == [
        "Casa",
        "Garage",
    ]
    assert await hass.config_entries.async_unload(entry_id)
