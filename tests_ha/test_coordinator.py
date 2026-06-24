"""Tests for the INIM Prime coordinator."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from custom_components.inim_prime.client import InimApiError, InimConnectionError, ApiStatus

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.const import (
    DEFAULT_SCAN_INTERVAL_ACTIVE,
    FAILURES_BEFORE_BACKOFF,
)
from custom_components.inim_prime.coordinator import (
    InimData,
    InimDataUpdateCoordinator,
)


def _make_coordinator(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    client: AsyncMock,
) -> InimDataUpdateCoordinator:
    mock_config_entry.add_to_hass(hass)
    return InimDataUpdateCoordinator(hass, mock_config_entry, client)


async def test_update_populates_inim_data(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    sample_version,
    sample_areas,
    sample_zones,
    sample_scenarios,
    sample_outputs,
    sample_fault,
    sample_api_stats,
) -> None:
    """A successful cycle populates a fully-formed InimData."""
    coordinator = _make_coordinator(hass, mock_config_entry, mock_client)

    data = await coordinator._async_update_data()

    assert isinstance(data, InimData)
    assert data.version == sample_version
    assert data.areas == sample_areas
    assert data.zones == sample_zones
    assert data.scenarios == sample_scenarios
    assert data.outputs == sample_outputs
    assert data.fault == sample_fault
    assert data.api_stats == sample_api_stats

    # version is fetched once and cached.
    await coordinator._async_update_data()
    mock_client.version.assert_awaited_once()


async def test_api_stats_failure_degrades_to_none(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """A failing api_stats read degrades to None, not a full failure."""
    mock_client.get_api_stats.side_effect = InimConnectionError("nope")
    coordinator = _make_coordinator(hass, mock_config_entry, mock_client)

    data = await coordinator._async_update_data()

    assert data.api_stats is None
    assert data.areas  # core data still present


async def test_api_stats_api_error_degrades_to_none(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """An InimApiError on api_stats also degrades to None."""
    mock_client.get_api_stats.side_effect = InimApiError(ApiStatus.NOT_IMPLEMENTED)
    coordinator = _make_coordinator(hass, mock_config_entry, mock_client)

    data = await coordinator._async_update_data()

    assert data.api_stats is None


async def test_core_connection_failure_raises_update_failed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """A core read connection failure raises UpdateFailed."""
    mock_client.get_areas.side_effect = InimConnectionError("down")
    coordinator = _make_coordinator(hass, mock_config_entry, mock_client)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_core_api_error_raises_update_failed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """A core read API error raises UpdateFailed."""
    mock_client.get_zones.side_effect = InimApiError(ApiStatus.ERROR_EXECUTION)
    coordinator = _make_coordinator(hass, mock_config_entry, mock_client)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_apikey_error_raises_config_entry_auth_failed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """An ERROR_APIKEY status triggers reauth via ConfigEntryAuthFailed."""
    mock_client.get_areas.side_effect = InimApiError(ApiStatus.ERROR_APIKEY)
    coordinator = _make_coordinator(hass, mock_config_entry, mock_client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_scan_interval_from_options(
    hass: HomeAssistant,
    mock_client: AsyncMock,
) -> None:
    """The coordinator's update interval comes from options when set."""
    entry = MockConfigEntry(
        domain="inim_prime",
        title="INIM Prime",
        data={
            "host": "192.0.2.10",
            "port": 8080,
            "apikey": "secret-key",
            "use_https": False,
            "scan_interval": 15,
        },
        options={"scan_interval": 42},
        unique_id="192.0.2.10:8080",
    )
    entry.add_to_hass(hass)
    coordinator = InimDataUpdateCoordinator(hass, entry, mock_client)

    assert coordinator.update_interval.total_seconds() == 42


# ---------------------------------------------------------------------------
# Fast defaults
# ---------------------------------------------------------------------------
async def test_default_active_interval_is_one_second(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """The default fast/active interval is 1s."""
    assert DEFAULT_SCAN_INTERVAL_ACTIVE == 1
    coordinator = _make_coordinator(hass, mock_config_entry, mock_client)
    assert coordinator._active_interval == timedelta(seconds=1)


# ---------------------------------------------------------------------------
# Per-cycle hard timeout: slow client never hangs the coordinator
# ---------------------------------------------------------------------------
async def test_slow_cycle_raises_update_failed_within_timeout(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cycle slower than DEFAULT_CYCLE_TIMEOUT aborts with UpdateFailed."""
    # Shrink the ceiling so the test is fast; the coordinator reads the module
    # constant at call time.
    import custom_components.inim_prime.coordinator as coord_mod

    monkeypatch.setattr(coord_mod, "DEFAULT_CYCLE_TIMEOUT", 0.2)

    async def _slow_get_areas() -> None:
        await asyncio.sleep(5)  # >> the (patched) cycle ceiling

    mock_client.get_areas.side_effect = _slow_get_areas
    coordinator = _make_coordinator(hass, mock_config_entry, mock_client)

    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(UpdateFailed):
        await asyncio.wait_for(coordinator._async_update_data(), timeout=2)
    elapsed = loop.time() - start

    # It aborted at the ceiling, not after the full 5s sleep.
    assert elapsed < 1.5


# ---------------------------------------------------------------------------
# No-overlap guard: a second refresh while one is in flight coalesces
# ---------------------------------------------------------------------------
async def test_overlapping_refresh_coalesces_no_concurrent_cycle(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """A refresh fired mid-cycle returns cached data, no concurrent cgi reads."""
    coordinator = _make_coordinator(hass, mock_config_entry, mock_client)

    # Seed a cached snapshot first.
    seed = await coordinator._async_update_data()
    coordinator.async_set_updated_data(seed)

    concurrency = {"current": 0, "max": 0}
    release = asyncio.Event()

    async def _gated_get_areas():
        concurrency["current"] += 1
        concurrency["max"] = max(concurrency["max"], concurrency["current"])
        await release.wait()
        concurrency["current"] -= 1
        return seed.areas

    mock_client.get_areas.side_effect = _gated_get_areas

    # Start one cycle and let it block inside get_areas.
    first = asyncio.create_task(coordinator._async_update_data())
    await asyncio.sleep(0)  # let `first` acquire the lock and enter get_areas
    while concurrency["current"] == 0:
        await asyncio.sleep(0)

    # A second refusal-to-overlap call must coalesce to cached data immediately.
    second = await coordinator._async_update_data()
    assert second is seed

    # Let the first cycle finish.
    release.set()
    await first

    # The gated read was never executed concurrently.
    assert concurrency["max"] == 1


# ---------------------------------------------------------------------------
# Failure backoff: relax to idle after N failures, recover on success
# ---------------------------------------------------------------------------
async def test_backoff_relaxes_to_idle_then_recovers(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """After FAILURES_BEFORE_BACKOFF failures, pin idle; recover on success."""
    coordinator = _make_coordinator(hass, mock_config_entry, mock_client)

    # Pretend we are in the fast tier (as if an event just arrived).
    coordinator.update_interval = coordinator._active_interval
    assert coordinator.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL_ACTIVE)

    mock_client.get_areas.side_effect = InimConnectionError("down")

    for i in range(FAILURES_BEFORE_BACKOFF):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        if i < FAILURES_BEFORE_BACKOFF - 1:
            assert not coordinator._backed_off

    # Now backed off and pinned to the idle tier.
    assert coordinator._backed_off
    assert coordinator.update_interval == coordinator._idle_interval

    # While backed off, a webhook event must NOT re-arm fast polling.
    coordinator.activate_fast_poll()
    assert coordinator.update_interval == coordinator._idle_interval
    assert coordinator._cancel_decay is None

    # A successful cycle clears the backoff and the counter.
    mock_client.get_areas.side_effect = None
    await coordinator._async_update_data()
    assert not coordinator._backed_off
    assert coordinator._consecutive_failures == 0

    # Fast polling can be armed again after recovery.
    coordinator.activate_fast_poll()
    assert coordinator.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL_ACTIVE)
    coordinator.async_cancel_decay()
