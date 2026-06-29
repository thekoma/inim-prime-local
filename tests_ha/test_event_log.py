"""Tests for the inim_prime.get_event_log action (read-only event log)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import Local6004Error
from custom_components.inim_prime.const import DOMAIN
from custom_components.inim_prime.event_log import (
    SERVICE_GET_EVENT_LOG,
    async_register_event_log_service,
)

_SAMPLE_EVENTS = [
    {
        "time": "2026-06-28 10:00:00",
        "event": "Valid key",
        "partitions": ["Home"],
        "restoral": False,
    },
    {
        "time": "2026-06-28 10:00:05",
        "event": "Disarm partition",
        "partitions": ["Home"],
        "restoral": False,
    },
    {
        "time": "2026-06-28 10:00:06",
        "event": "Scenario",
        "partitions": [],
        "restoral": False,
        "scenario": "Dis.Box",
    },
]


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Set up the entry and return one of its entity ids."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    reg = er.async_get(hass)
    ents = er.async_entries_for_config_entry(reg, entry.entry_id)
    return ents[0].entity_id


async def _call(hass: HomeAssistant, data: dict, *, response: bool = True):
    return await hass.services.async_call(
        DOMAIN, SERVICE_GET_EVENT_LOG, data, blocking=True, return_response=response
    )


async def test_get_event_log_returns_decoded_events(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """The action returns the decoded log; limit keeps the most recent N."""
    eid = await _setup(hass, mock_config_entry)
    mock_config_entry.runtime_data.local_client.async_read_event_log = AsyncMock(
        return_value=list(_SAMPLE_EVENTS)
    )

    result = await _call(hass, {"entity_id": eid, "limit": 2})
    assert result["count"] == 2
    assert [e["event"] for e in result["events"]] == ["Disarm partition", "Scenario"]


async def test_get_event_log_limit_zero_returns_all(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    eid = await _setup(hass, mock_config_entry)
    mock_config_entry.runtime_data.local_client.async_read_event_log = AsyncMock(
        return_value=list(_SAMPLE_EVENTS)
    )
    result = await _call(hass, {"entity_id": eid, "limit": 0})
    assert result["count"] == 3


async def test_get_event_log_read_error_raises(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    eid = await _setup(hass, mock_config_entry)
    mock_config_entry.runtime_data.local_client.async_read_event_log = AsyncMock(
        side_effect=Local6004Error("down")
    )
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, {"entity_id": eid})
    assert exc.value.translation_key == "command_failed"


async def test_get_event_log_missing_local_client(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    eid = await _setup(hass, mock_config_entry)
    mock_config_entry.runtime_data.local_client = None
    with pytest.raises(HomeAssistantError):
        await _call(hass, {"entity_id": eid})


async def test_get_event_log_bad_target(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    await _setup(hass, mock_config_entry)
    with pytest.raises(ServiceValidationError) as exc:
        await _call(hass, {"entity_id": "sensor.not_inim"})
    assert exc.value.translation_key == "event_log_bad_target"


async def test_get_event_log_single_target(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    eid = await _setup(hass, mock_config_entry)
    with pytest.raises(ServiceValidationError) as exc:
        await _call(hass, {"entity_id": [eid, "sensor.other_panel"]})
    assert exc.value.translation_key == "event_log_single_target"


async def test_get_event_log_foreign_entity(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """An entity owned by a different (non-inim) entry is rejected."""
    await _setup(hass, mock_config_entry)  # registers the service
    other = MockConfigEntry(domain="other_domain", title="X")
    other.add_to_hass(hass)
    reg = er.async_get(hass)
    foreign = reg.async_get_or_create(
        "sensor", "other_domain", "uid-1", config_entry=other
    )
    with pytest.raises(ServiceValidationError) as exc:
        await _call(hass, {"entity_id": foreign.entity_id})
    assert exc.value.translation_key == "event_log_bad_target"


async def test_register_event_log_service_idempotent(hass: HomeAssistant) -> None:
    """Registering twice is a no-op (the second call returns early)."""
    async_register_event_log_service(hass)
    async_register_event_log_service(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_GET_EVENT_LOG)
