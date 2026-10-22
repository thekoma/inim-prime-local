"""Tests for the live area and zone state read natively (TCP 6004)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import (
    AreaMode,
    AreaState,
    Local6004Config,
    NativeAreaStatus,
    NativeZoneStatus,
    ZoneState,
)
from custom_components.inim_prime.coordinator import InimDataUpdateCoordinator

# conftest: one area (id 1) and one zone (id 1, in area 1).
DISARMED = NativeAreaStatus(mode=AreaMode.DISARMED, alarm=False, alarm_memory=False)


async def _coordinator(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: AsyncMock,
    config: Local6004Config,
) -> InimDataUpdateCoordinator:
    entry.add_to_hass(hass)
    coordinator = InimDataUpdateCoordinator(hass, entry, client, config)
    await coordinator.async_refresh()
    return coordinator


@pytest.mark.parametrize(
    ("status", "mode", "state", "memory"),
    [
        (DISARMED, AreaMode.DISARMED, AreaState.READY, False),
        (
            NativeAreaStatus(mode=AreaMode.TOTAL, alarm=False, alarm_memory=False),
            AreaMode.TOTAL,
            AreaState.READY,
            False,
        ),
        (
            NativeAreaStatus(mode=AreaMode.TOTAL, alarm=True, alarm_memory=True),
            AreaMode.TOTAL,
            AreaState.ALARM,
            True,
        ),
        # Retained memory alone does not make the area alarming.
        (
            NativeAreaStatus(mode=AreaMode.TOTAL, alarm=False, alarm_memory=True),
            AreaMode.TOTAL,
            AreaState.READY,
            True,
        ),
        (
            NativeAreaStatus(
                mode=AreaMode.DISARMED, alarm=False, alarm_memory=False, sabotage=True
            ),
            AreaMode.DISARMED,
            AreaState.SABOTAGE,
            False,
        ),
        # An alarm outranks a sabotage.
        (
            NativeAreaStatus(mode=AreaMode.PARTIAL, alarm=True, alarm_memory=True, sabotage=True),
            AreaMode.PARTIAL,
            AreaState.ALARM,
            True,
        ),
    ],
)
async def test_area_state_from_the_partition_status(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    area_statuses: dict[int, NativeAreaStatus],
    status: NativeAreaStatus,
    mode: AreaMode,
    state: AreaState,
    memory: bool,
) -> None:
    area_statuses[1] = status
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, mock_local_config)

    area = coordinator.data.areas[0]
    assert (area.id, area.label) == (1, "Home")
    assert (area.mode, area.state, area.alarm_memory) == (mode, state, memory)


async def test_alarm_then_clear(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    area_statuses: dict[int, NativeAreaStatus],
) -> None:
    """An alarm shows on the next poll, and so does its reset."""
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, mock_local_config)

    area_statuses[1] = NativeAreaStatus(mode=AreaMode.TOTAL, alarm=True, alarm_memory=True)
    await coordinator.async_refresh()
    assert coordinator.data.areas[0].state is AreaState.ALARM

    area_statuses[1] = DISARMED
    await coordinator.async_refresh()
    assert coordinator.data.areas[0].state is AreaState.READY
    assert not coordinator.data.areas[0].alarm_memory


async def test_missing_status_keeps_the_last_state(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    area_statuses: dict[int, NativeAreaStatus],
    zone_statuses: dict[int, NativeZoneStatus],
) -> None:
    """An area or zone the reading leaves out keeps its last known state."""
    area_statuses[1] = NativeAreaStatus(mode=AreaMode.TOTAL, alarm=False, alarm_memory=False)
    zone_statuses[1] = NativeZoneStatus(state=ZoneState.ALARM, excluded=True, alarm_memory=True)
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, mock_local_config)

    area_statuses.clear()
    zone_statuses.clear()
    await coordinator.async_refresh()

    assert coordinator.data.areas[0].mode is AreaMode.TOTAL
    zone = coordinator.data.zones[0]
    assert (zone.state, zone.excluded, zone.alarm_memory) == (ZoneState.ALARM, True, True)


async def test_before_any_status_objects_are_neutral(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    area_statuses: dict[int, NativeAreaStatus],
    zone_statuses: dict[int, NativeZoneStatus],
) -> None:
    """Without a first reading an area is disarmed and ready, a zone closed."""
    area_statuses.clear()
    zone_statuses.clear()
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, mock_local_config)

    area = coordinator.data.areas[0]
    assert (area.mode, area.state, area.alarm_memory) == (
        AreaMode.DISARMED,
        AreaState.READY,
        False,
    )
    zone = coordinator.data.zones[0]
    assert (zone.state, zone.excluded, zone.alarm_memory) == (ZoneState.READY, False, False)


async def test_zone_state_from_the_terminal_status(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    zone_statuses: dict[int, NativeZoneStatus],
) -> None:
    """Only the zones of the structure are read, and each is mapped one to one."""
    zone_statuses[1] = NativeZoneStatus(state=ZoneState.ALARM, excluded=True, alarm_memory=True)
    zone_statuses[99] = NativeZoneStatus(state=ZoneState.ALARM, excluded=False, alarm_memory=False)
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, mock_local_config)

    mock_local_client.async_get_zone_statuses.assert_awaited_with({1})
    assert [z.id for z in coordinator.data.zones] == [1]
    zone = coordinator.data.zones[0]
    assert (zone.label, zone.terminal) == ("Front Door", 1)
    assert (zone.state, zone.excluded, zone.alarm_memory) == (ZoneState.ALARM, True, True)
