"""Tests for the fast native (TCP 6004) area-state path."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.inim_prime.client import (
    AreaMode,
    AreaState,
    Local6004Error,
    NativeAreaStatus,
)
from custom_components.inim_prime.const import (
    CONF_NATIVE_AREA_POLL,
    NATIVE_AREA_BACKOFF_TICKS,
    NATIVE_AREA_FAILURES_BEFORE_BACKOFF,
    NATIVE_AREA_POLL_INTERVAL,
)
from custom_components.inim_prime.coordinator import InimDataUpdateCoordinator

# sample_areas (conftest) has a single area: id=1, DISARMED, READY.
ARMED = NativeAreaStatus(mode=AreaMode.TOTAL, alarm=False, alarm_memory=False)
DISARMED = NativeAreaStatus(mode=AreaMode.DISARMED, alarm=False, alarm_memory=False)
ALARM = NativeAreaStatus(mode=AreaMode.TOTAL, alarm=True, alarm_memory=True)


async def _coordinator(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    native: AsyncMock | None,
) -> InimDataUpdateCoordinator:
    mock_config_entry.add_to_hass(hass)
    coordinator = InimDataUpdateCoordinator(hass, mock_config_entry, mock_client)
    coordinator.async_set_updated_data(await coordinator._async_update_data())
    coordinator.native_client = native
    return coordinator


async def test_native_change_is_published_and_arms_fast_poll(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """A native mode change patches the snapshot and arms the fast cgi poll."""
    native = AsyncMock()
    native.async_get_area_statuses.return_value = {1: ARMED}
    coordinator = await _coordinator(hass, mock_config_entry, mock_client, native)

    await coordinator.async_native_area_poll()

    assert coordinator.data.areas[0].mode is AreaMode.TOTAL
    assert coordinator.update_interval == coordinator._active_interval
    coordinator.async_cancel_decay()


async def test_native_unchanged_state_does_not_publish(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """Matching native state (or unknown areas) leaves the snapshot alone."""
    native = AsyncMock()
    native.async_get_area_statuses.return_value = {1: DISARMED, 7: ARMED}
    coordinator = await _coordinator(hass, mock_config_entry, mock_client, native)
    before = coordinator.data

    await coordinator.async_native_area_poll()

    assert coordinator.data is before
    assert coordinator.update_interval == coordinator._idle_interval


def test_apply_native_statuses_without_snapshot(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """Without a cached snapshot there is nothing to patch."""
    coordinator = InimDataUpdateCoordinator(hass, mock_config_entry, mock_client)
    assert coordinator.apply_native_statuses({1: ALARM}) is None  # no snapshot yet


async def test_native_alarm_then_clear(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """An alarm sets state ALARM + memory; clearing it restores READY."""
    coordinator = await _coordinator(hass, mock_config_entry, mock_client, None)

    alarmed = coordinator.apply_native_statuses({1: ALARM})
    assert alarmed is not None
    assert alarmed.areas[0].state is AreaState.ALARM
    assert alarmed.areas[0].alarm_memory
    coordinator.async_set_updated_data(alarmed)

    cleared = coordinator.apply_native_statuses({1: DISARMED})
    assert cleared is not None
    assert cleared.areas[0].state is AreaState.READY
    assert not cleared.areas[0].alarm_memory


async def test_native_poll_noop_without_client_or_data(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """No native client, or no snapshot yet, means nothing to do."""
    coordinator = await _coordinator(hass, mock_config_entry, mock_client, None)
    await coordinator.async_native_area_poll()  # no client

    native = AsyncMock()
    fresh = InimDataUpdateCoordinator(hass, mock_config_entry, mock_client)
    fresh.native_client = native
    await fresh.async_native_area_poll()  # no data
    native.async_get_area_statuses.assert_not_awaited()


async def test_native_failures_back_off_then_recover(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """Repeated failures skip ticks; entities are never made unavailable."""
    native = AsyncMock()
    native.async_get_area_statuses.side_effect = Local6004Error("down")
    coordinator = await _coordinator(hass, mock_config_entry, mock_client, native)

    for _ in range(NATIVE_AREA_FAILURES_BEFORE_BACKOFF):
        await coordinator.async_native_area_poll()
    assert coordinator.last_update_success
    calls = native.async_get_area_statuses.await_count

    for _ in range(NATIVE_AREA_BACKOFF_TICKS):
        await coordinator.async_native_area_poll()
    assert native.async_get_area_statuses.await_count == calls  # skipped

    native.async_get_area_statuses.side_effect = None
    native.async_get_area_statuses.return_value = {1: ARMED}
    await coordinator.async_native_area_poll()
    assert coordinator._native_failures == 0
    assert coordinator.data.areas[0].mode is AreaMode.TOTAL
    coordinator.async_cancel_decay()


async def test_setup_starts_native_poll_and_unload_closes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    mock_local_client: AsyncMock,
) -> None:
    """With the option on (default) the timer polls natively; unload closes."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_config_entry.runtime_data.coordinator
    assert coordinator.native_client is mock_local_client

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=NATIVE_AREA_POLL_INTERVAL + 1)
    )
    await hass.async_block_till_done()
    mock_local_client.async_get_area_statuses.assert_awaited()

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_local_client.async_close.assert_awaited_once()


async def test_setup_native_poll_disabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    mock_local_client: AsyncMock,
) -> None:
    """With the option off no native client is attached."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_NATIVE_AREA_POLL: False}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.runtime_data.coordinator.native_client is None
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    mock_local_client.async_close.assert_not_awaited()
