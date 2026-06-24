"""Tests for the INIM Prime select platform."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeassistant.exceptions import ServiceValidationError

from custom_components.inim_prime.client import Scenario, Version

from custom_components.inim_prime.coordinator import InimData
from custom_components.inim_prime.select import InimScenarioSelect


def _make_coordinator(scenarios: list[Scenario]) -> SimpleNamespace:
    """Build a fake coordinator carrying sample InimData."""
    version = Version(
        version="4.07", verhttp="1.0", primex="4.07 PX500", servizio=False
    )
    data = InimData(
        version=version,
        areas=[],
        zones=[],
        scenarios=scenarios,
        outputs=[],
        fault=None,
        api_stats=None,
    )
    coordinator = SimpleNamespace()
    coordinator.data = data
    coordinator.client = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()
    coordinator.config_entry = SimpleNamespace(entry_id="abc123", title="INIM Prime")
    # CoordinatorEntity.__init__ touches these attributes.
    coordinator.async_add_listener = lambda *a, **k: lambda: None
    coordinator.last_update_success = True
    return coordinator


def test_options_and_unique_id_and_device_info() -> None:
    """Options list scenario labels; unique_id and device_info follow the contract."""
    scenarios = [
        Scenario(id=1, label="Away", active=False),
        Scenario(id=2, label="Night", active=False),
    ]
    coordinator = _make_coordinator(scenarios)
    entity = InimScenarioSelect(coordinator)

    assert entity.options == ["Away", "Night"]
    # Fixed-name entity: name comes from the translation key.
    assert entity.translation_key == "active_scenario"
    assert entity.unique_id == "abc123_select_scenario"
    assert entity.device_info["identifiers"] == {("inim_prime", "abc123")}
    assert entity.device_info["model"] == "4.07 PX500"
    assert entity.device_info["sw_version"] == "4.07"
    assert entity.device_info["name"] == "INIM Prime"


def test_current_option_active() -> None:
    """current_option returns the active scenario's label."""
    scenarios = [
        Scenario(id=1, label="Away", active=False),
        Scenario(id=2, label="Night", active=True),
    ]
    entity = InimScenarioSelect(_make_coordinator(scenarios))
    assert entity.current_option == "Night"


def test_current_option_none_when_no_active() -> None:
    """current_option is None when no scenario is active."""
    scenarios = [Scenario(id=1, label="Away", active=False)]
    entity = InimScenarioSelect(_make_coordinator(scenarios))
    assert entity.current_option is None


@pytest.mark.asyncio
async def test_select_option_applies_scenario_and_refreshes() -> None:
    """Selecting a label applies the matching scenario id then refreshes."""
    scenarios = [
        Scenario(id=1, label="Away", active=False),
        Scenario(id=2, label="Night", active=False),
    ]
    coordinator = _make_coordinator(scenarios)
    entity = InimScenarioSelect(coordinator)

    await entity.async_select_option("Night")

    coordinator.client.apply_scenario.assert_awaited_once_with(2)
    coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_select_unknown_option_raises() -> None:
    """An unknown label raises ServiceValidationError and does not refresh."""
    scenarios = [Scenario(id=1, label="Away", active=False)]
    coordinator = _make_coordinator(scenarios)
    entity = InimScenarioSelect(coordinator)

    with pytest.raises(ServiceValidationError) as exc_info:
        await entity.async_select_option("Nonexistent")
    assert exc_info.value.translation_key == "invalid_scenario"

    coordinator.client.apply_scenario.assert_not_awaited()
    coordinator.async_request_refresh.assert_not_awaited()
