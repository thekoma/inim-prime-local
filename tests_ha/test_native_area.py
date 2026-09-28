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
    NativeZoneStatus,
    ZoneState,
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
    if native is not None and not isinstance(native.async_get_zone_statuses.return_value, dict):
        native.async_get_zone_statuses.return_value = {}
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

    await coordinator.async_native_poll()

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

    await coordinator.async_native_poll()

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
    await coordinator.async_native_poll()  # no client

    native = AsyncMock()
    fresh = InimDataUpdateCoordinator(hass, mock_config_entry, mock_client)
    fresh.native_client = native
    await fresh.async_native_poll()  # no data
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
        await coordinator.async_native_poll()
    assert coordinator.last_update_success
    calls = native.async_get_area_statuses.await_count

    for _ in range(NATIVE_AREA_BACKOFF_TICKS):
        await coordinator.async_native_poll()
    assert native.async_get_area_statuses.await_count == calls  # skipped

    native.async_get_area_statuses.side_effect = None
    native.async_get_area_statuses.return_value = {1: ARMED}
    await coordinator.async_native_poll()
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


async def test_native_memory_only_does_not_raise_alarm(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """Retained alarm memory on an armed area does not make it ALARM."""
    coordinator = await _coordinator(hass, mock_config_entry, mock_client, None)
    patched = coordinator.apply_native_statuses(
        {1: NativeAreaStatus(mode=AreaMode.TOTAL, alarm=False, alarm_memory=True)}
    )
    assert patched is not None
    assert patched.areas[0].state is AreaState.READY
    assert patched.areas[0].alarm_memory


# sample_zones (conftest) has a single zone: id=1, READY, not excluded.
OPEN = NativeZoneStatus(state=ZoneState.ALARM, excluded=False, alarm_memory=False)
READY = NativeZoneStatus(state=ZoneState.READY, excluded=False, alarm_memory=False)


async def test_native_zone_change_published_without_fast_poll(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """A zone change is published but does not push the cgi to its fast tier."""
    native = AsyncMock()
    native.async_get_area_statuses.return_value = {}
    native.async_get_zone_statuses.return_value = {1: OPEN}
    coordinator = await _coordinator(hass, mock_config_entry, mock_client, native)

    await coordinator.async_native_poll()

    native.async_get_zone_statuses.assert_awaited_once_with({1})
    assert coordinator.data.zones[0].state is ZoneState.ALARM
    assert coordinator.update_interval == coordinator._idle_interval


async def test_native_area_and_zone_change_together(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """Area and zone changes land in one published snapshot."""
    native = AsyncMock()
    native.async_get_area_statuses.return_value = {1: ARMED}
    native.async_get_zone_statuses.return_value = {
        1: NativeZoneStatus(state=ZoneState.READY, excluded=True, alarm_memory=True)
    }
    coordinator = await _coordinator(hass, mock_config_entry, mock_client, native)

    await coordinator.async_native_poll()

    assert coordinator.data.areas[0].mode is AreaMode.TOTAL
    assert coordinator.data.zones[0].excluded
    assert coordinator.data.zones[0].alarm_memory
    coordinator.async_cancel_decay()


async def test_native_zone_unchanged_or_unknown(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """Matching or unknown zones leave the snapshot untouched."""
    coordinator = await _coordinator(hass, mock_config_entry, mock_client, None)
    assert coordinator.apply_native_zones(coordinator.data, {1: READY, 99: OPEN}) is None


async def test_native_poll_skips_zone_read_without_zones(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """With no zones in the snapshot the terminal read is not issued."""
    mock_client.get_zones.return_value = []
    native = AsyncMock()
    native.async_get_area_statuses.return_value = {}
    coordinator = await _coordinator(hass, mock_config_entry, mock_client, native)

    await coordinator.async_native_poll()

    native.async_get_zone_statuses.assert_not_awaited()


async def test_native_update_does_not_reschedule_cgi(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """Native changes notify listeners without rescheduling the cgi refresh."""
    native = AsyncMock()
    native.async_get_area_statuses.return_value = {}
    native.async_get_zone_statuses.return_value = {1: OPEN}
    coordinator = await _coordinator(hass, mock_config_entry, mock_client, native)
    calls: list[None] = []
    unsub = coordinator.async_add_listener(lambda: calls.append(None))
    coordinator.async_set_updated_data = None  # type: ignore[assignment,method-assign]

    await coordinator.async_native_poll()  # would raise if async_set_updated_data were used

    assert calls
    assert coordinator.data.zones[0].state is ZoneState.ALARM
    unsub()


async def test_cgi_cycle_does_not_roll_back_fresher_native_state(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    sample_areas,
) -> None:
    """A native read taken during a cgi cycle wins over that cycle's stale data."""
    native = AsyncMock()
    native.async_get_area_statuses.return_value = {1: ARMED}
    native.async_get_zone_statuses.return_value = {1: OPEN}
    coordinator = await _coordinator(hass, mock_config_entry, mock_client, native)

    async def _stale_areas_with_concurrent_native_read():
        await coordinator.async_native_poll()  # the panel changed mid-cycle
        return sample_areas  # the cgi still reports DISARMED

    mock_client.get_areas.side_effect = _stale_areas_with_concurrent_native_read
    data = await coordinator._async_update_data()

    assert data.areas[0].mode is AreaMode.TOTAL
    assert data.zones[0].state is ZoneState.ALARM
    coordinator.async_cancel_decay()


async def test_cgi_cycle_wins_over_older_native_state(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """A native read taken before the cgi cycle started does not override it."""
    native = AsyncMock()
    native.async_get_area_statuses.return_value = {1: ARMED}
    coordinator = await _coordinator(hass, mock_config_entry, mock_client, native)
    await coordinator.async_native_poll()  # native says armed, before the cycle
    coordinator.async_cancel_decay()

    data = await coordinator._async_update_data()  # cgi (newer) says disarmed

    assert data.areas[0].mode is AreaMode.DISARMED
