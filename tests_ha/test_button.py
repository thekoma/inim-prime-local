"""Tests for the INIM Prime button platform."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from custom_components.inim_prime.client import AreaMode, AreaState, Area

from homeassistant.const import EntityCategory

from custom_components.inim_prime.button import (
    InimClearAlarmMemoryButton,
    async_setup_entry,
)
from custom_components.inim_prime.const import DOMAIN
from custom_components.inim_prime.coordinator import InimData


def _make_coordinator(
    sample_version,
    areas: list[Area],
) -> MagicMock:
    """Build a fake coordinator carrying sample InimData."""
    coordinator = MagicMock()
    coordinator.client = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()
    coordinator.config_entry = MagicMock(entry_id="abc123", title="INIM Prime")
    coordinator.data = InimData(
        version=sample_version,
        areas=areas,
        zones=[],
        scenarios=[],
        outputs=[],
        fault=MagicMock(),
        api_stats=None,
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


async def test_async_setup_entry_creates_one_button_per_area(
    sample_version, two_areas
) -> None:
    """One button entity is created per area."""
    coordinator = _make_coordinator(sample_version, two_areas)
    entry = MagicMock()
    entry.runtime_data.coordinator = coordinator

    added: list = []
    await async_setup_entry(
        MagicMock(), entry, lambda new, *a, **k: added.extend(list(new))
    )

    assert len(added) == 2
    assert all(isinstance(e, InimClearAlarmMemoryButton) for e in added)


def test_button_properties(sample_version, two_areas) -> None:
    """The button exposes the expected identity and category."""
    coordinator = _make_coordinator(sample_version, two_areas)
    button = InimClearAlarmMemoryButton(coordinator, two_areas[0])

    assert button.unique_id == "abc123_button_1"
    assert button.name == "Home clear alarm memory"
    assert button.entity_category is EntityCategory.CONFIG
    assert button.has_entity_name is True

    device = button.device_info
    assert device["identifiers"] == {(DOMAIN, "abc123")}
    assert device["manufacturer"] == "INIM"
    assert device["model"] == sample_version.primex
    assert device["sw_version"] == sample_version.version
    assert device["name"] == "INIM Prime"


def test_button_enabled_default_by_area_name(sample_version) -> None:
    """Buttons for factory-default areas are hidden; real ones stay enabled."""
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
    coordinator = _make_coordinator(sample_version, areas)

    factory_button = InimClearAlarmMemoryButton(coordinator, areas[0])
    real_button = InimClearAlarmMemoryButton(coordinator, areas[1])

    assert factory_button.entity_registry_enabled_default is False
    assert real_button.entity_registry_enabled_default is True


async def test_async_press_calls_client_then_refreshes(
    sample_version, two_areas
) -> None:
    """Pressing clears the area's alarm memory then requests a refresh."""
    coordinator = _make_coordinator(sample_version, two_areas)
    button = InimClearAlarmMemoryButton(coordinator, two_areas[1])

    await button.async_press()

    coordinator.client.clear_alarm_memory.assert_awaited_once_with(2)
    coordinator.async_request_refresh.assert_awaited_once()
