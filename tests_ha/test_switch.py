"""Tests for the INIM Prime switch platform."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from custom_components.inim_prime.client import (
    ApiStatus,
    Fault,
    InimApiError,
    Output,
    Version,
    Zone,
    ZoneState,
)
from custom_components.inim_prime.coordinator import InimData
from custom_components.inim_prime.switch import (
    InimOutputSwitch,
    InimZoneBypassSwitch,
    async_setup_entry,
)


@pytest.fixture
def version() -> Version:
    return Version(version="4.07", verhttp="1.0", primex="4.07 PX500", servizio=False)


@pytest.fixture
def outputs() -> list[Output]:
    return [
        Output(id=1, label="Siren", terminal=1, state=0, type=0),
        Output(id=2, label="Gate", terminal=2, state=3, type=0),
    ]


@pytest.fixture
def zones() -> list[Zone]:
    return [
        Zone(
            id=1,
            label="Front Door",
            terminal=1,
            state=ZoneState.READY,
            alarm_memory=False,
            excluded=False,
        ),
        Zone(
            id=2,
            label="Kitchen",
            terminal=2,
            state=ZoneState.READY,
            alarm_memory=False,
            excluded=True,
        ),
    ]


@pytest.fixture
def fake_coordinator(version, outputs, zones):
    """Build a fake coordinator with sample InimData and an async client."""
    data = InimData(
        version=version,
        areas=[],
        zones=zones,
        scenarios=[],
        outputs=outputs,
        fault=Fault(vcc=13.5, raw_fau="0", has_faults=False),
        api_stats=None,
    )
    client = SimpleNamespace(
        set_output=AsyncMock(),
        set_zone_excluded=AsyncMock(),
    )
    coordinator = SimpleNamespace(
        data=data,
        client=client,
        config_entry=SimpleNamespace(entry_id="abc123", title="INIM Prime", options={}),
        hass=SimpleNamespace(config=SimpleNamespace(language="en")),
        local_config=None,
        last_update_success=True,
        async_request_refresh=AsyncMock(),
        async_add_listener=lambda *a, **k: lambda: None,
    )
    return coordinator


@pytest.fixture
def entry():
    return SimpleNamespace(entry_id="abc123", title="INIM Prime")


def test_output_switch_state_mapping(fake_coordinator, entry):
    off = InimOutputSwitch(fake_coordinator, entry, 1)
    on = InimOutputSwitch(fake_coordinator, entry, 2)

    assert off.is_on is False
    assert on.is_on is True
    assert off.name == "Siren"
    assert off.unique_id == "abc123_output_1"
    assert off.entity_category is None
    di = off.device_info
    assert di["identifiers"] == {("inim_prime", "abc123")}
    assert di["manufacturer"] == "INIM"
    assert di["model"] == "PX500"
    assert di["sw_version"] == "4.07"
    assert di["name"] == "INIM Prime"


def test_zone_bypass_switch(fake_coordinator, entry):
    not_bypassed = InimZoneBypassSwitch(fake_coordinator, entry, 1)
    bypassed = InimZoneBypassSwitch(fake_coordinator, entry, 2)

    assert not_bypassed.is_on is False
    assert bypassed.is_on is True
    assert not_bypassed.name == "Front Door bypass"
    assert not_bypassed.unique_id == "abc123_zone_bypass_1"
    assert not_bypassed.entity_category is EntityCategory.CONFIG


def test_zone_bypass_follows_zone_room(fake_coordinator, entry):
    """The bypass switch is grouped under the same per-room device as its zone."""
    # "Kitchen" resolves to a room; "Front Door" does not.
    kitchen = InimZoneBypassSwitch(fake_coordinator, entry, 2)
    front = InimZoneBypassSwitch(fake_coordinator, entry, 1)
    assert kitchen.device_info["identifiers"] == {("inim_prime", "abc123_room_kitchen")}
    assert kitchen.device_info["name"] == "Kitchen"
    assert front.device_info["identifiers"] == {("inim_prime", "abc123")}


async def test_output_turn_on_off(fake_coordinator, entry):
    sw = InimOutputSwitch(fake_coordinator, entry, 1)

    await sw.async_turn_on()
    fake_coordinator.client.set_output.assert_awaited_with(1, 1)

    await sw.async_turn_off()
    fake_coordinator.client.set_output.assert_awaited_with(1, 0)

    assert fake_coordinator.async_request_refresh.await_count == 2


async def test_output_code_not_allowed_raises_and_no_refresh(fake_coordinator, entry):
    fake_coordinator.client.set_output.side_effect = InimApiError(ApiStatus.CODE_NOT_ALLOWED)
    sw = InimOutputSwitch(fake_coordinator, entry, 1)

    with pytest.raises(HomeAssistantError) as exc_info:
        await sw.async_turn_on()
    assert exc_info.value.translation_key == "output_code_not_allowed"

    # State not flipped; no optimistic refresh requested.
    assert fake_coordinator.async_request_refresh.await_count == 0


async def test_output_other_api_error_raises(fake_coordinator, entry):
    fake_coordinator.client.set_output.side_effect = InimApiError(ApiStatus.ERROR_EXECUTION)
    sw = InimOutputSwitch(fake_coordinator, entry, 1)

    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()


async def test_zone_bypass_turn_on_off(fake_coordinator, entry):
    sw = InimZoneBypassSwitch(fake_coordinator, entry, 1)

    await sw.async_turn_on()
    fake_coordinator.client.set_zone_excluded.assert_awaited_with(1, True)

    await sw.async_turn_off()
    fake_coordinator.client.set_zone_excluded.assert_awaited_with(1, False)

    assert fake_coordinator.async_request_refresh.await_count == 2


def test_output_switch_disabled_by_default(fake_coordinator, entry):
    """Output switches are hidden/disabled by default (declutter)."""
    sw = InimOutputSwitch(fake_coordinator, entry, 1)
    assert sw.entity_registry_enabled_default is False


def test_zone_bypass_switch_enabled_by_default(fake_coordinator, entry):
    """Bypass switches stay enabled by default."""
    sw = InimZoneBypassSwitch(fake_coordinator, entry, 1)
    assert sw.entity_registry_enabled_default is True


async def test_async_setup_entry_creates_all_entities(fake_coordinator, entry):
    entry.runtime_data = SimpleNamespace(coordinator=fake_coordinator)
    entry.async_on_unload = lambda unsub: None
    added: list = []

    def add(entities):
        added.extend(entities)

    await async_setup_entry(None, entry, add)

    assert sum(isinstance(e, InimOutputSwitch) for e in added) == 2
    assert sum(isinstance(e, InimZoneBypassSwitch) for e in added) == 2
