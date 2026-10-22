"""Tests for the INIM Prime coordinator (native TCP 6004 only)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import (
    FAULT_FLAG_KEYS,
    GSM_NO_MODULE,
    GSM_NOT_INITIALISED,
    Area,
    AreaMode,
    AreaState,
    Fault,
    Gsm,
    Local6004Config,
    Local6004Error,
    Local6004Structure,
    NativeAreaStatus,
    NativeGsmStatus,
    NativeObject,
    NativePowerValues,
    NativeZoneDef,
    NativeZoneStatus,
    Output,
    PanelStatus,
    Power,
    Scenario,
    SceneDef,
    Version,
    Zone,
    ZoneState,
)
from custom_components.inim_prime.const import (
    FAILURES_BEFORE_BACKOFF,
    FAILURES_BEFORE_UNAVAILABLE,
    GSM_POLL_INTERVAL,
    NATIVE_BACKOFF_INTERVAL,
    NATIVE_POLL_INTERVAL,
    SLOW_POLL_INTERVAL,
)
from custom_components.inim_prime.coordinator import (
    InimData,
    InimDataUpdateCoordinator,
)


def _make_coordinator(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: AsyncMock,
    config: Local6004Config,
) -> InimDataUpdateCoordinator:
    entry.add_to_hass(hass)
    return InimDataUpdateCoordinator(hass, entry, client, config)


def _with_structure(config: Local6004Config, **kinds) -> Local6004Config:
    """Return ``config`` with some structure kinds replaced."""
    return replace(config, structure=replace(config.structure, **kinds))


async def test_cycle_builds_the_snapshot(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """One cycle reads the live statuses and builds every object of the structure."""
    coordinator = _make_coordinator(hass, mock_config_entry, mock_local_client, mock_local_config)

    data = await coordinator._async_update_data()

    assert isinstance(data, InimData)
    assert data.areas == [Area(1, "Home", AreaMode.DISARMED, AreaState.READY, False)]
    assert data.zones == [Zone(1, "Front Door", 1, ZoneState.READY, False, False)]
    # Scenario 1's definition disarms area 1, which is disarmed: active.
    assert data.scenarios == [Scenario(1, "Away", True)]
    assert data.outputs == [Output(1, "Siren", 0)]
    assert data.fault == Fault(
        vcc=13.7,
        has_faults=False,
        flags=dict.fromkeys(FAULT_FLAG_KEYS, False),
        raw="00" * 12,
    )
    assert data.version == Version(raw="4.07 PX020")
    mock_local_client.async_get_zone_statuses.assert_awaited_once_with({1})
    mock_local_client.async_get_output_states.assert_awaited_once_with({1})
    mock_local_client.async_get_system_status.assert_awaited_once()
    assert coordinator.update_interval == timedelta(seconds=NATIVE_POLL_INTERVAL)


async def test_area_alarm_and_sabotage_states(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    area_statuses: dict[int, NativeAreaStatus],
) -> None:
    """The partition state maps to ALARM, SABOTAGE or READY (alarm first)."""
    config = _with_structure(
        mock_local_config,
        areas=[NativeObject(1, "A"), NativeObject(2, "B"), NativeObject(3, "C")],
    )
    area_statuses.update(
        {
            1: NativeAreaStatus(AreaMode.TOTAL, alarm=True, alarm_memory=True, sabotage=True),
            2: NativeAreaStatus(AreaMode.PARTIAL, alarm=False, alarm_memory=False, sabotage=True),
            3: NativeAreaStatus(AreaMode.DISARMED, alarm=False, alarm_memory=True),
        }
    )
    coordinator = _make_coordinator(hass, mock_config_entry, mock_local_client, config)

    data = await coordinator._async_update_data()

    assert [(a.mode, a.state, a.alarm_memory) for a in data.areas] == [
        (AreaMode.TOTAL, AreaState.ALARM, True),
        (AreaMode.PARTIAL, AreaState.SABOTAGE, False),
        (AreaMode.DISARMED, AreaState.READY, True),
    ]


async def test_live_reading_cannot_add_objects(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    area_statuses: dict[int, NativeAreaStatus],
    zone_statuses: dict[int, NativeZoneStatus],
) -> None:
    """Only the structure decides which objects exist; extra statuses are ignored."""
    area_statuses[7] = NativeAreaStatus(AreaMode.TOTAL, alarm=False, alarm_memory=False)
    zone_statuses[9] = NativeZoneStatus(ZoneState.ALARM, excluded=False, alarm_memory=False)
    mock_local_client.async_get_output_states.return_value = {1: 0, 1505: 1}
    coordinator = _make_coordinator(
        hass, mock_config_entry, mock_local_client, mock_local_config
    )

    data = await coordinator._async_update_data()

    assert [a.id for a in data.areas] == [1]
    assert [z.id for z in data.zones] == [1]
    assert [o.id for o in data.outputs] == [1]


async def test_missing_status_keeps_the_last_state_or_a_neutral_one(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    area_statuses: dict[int, NativeAreaStatus],
    zone_statuses: dict[int, NativeZoneStatus],
) -> None:
    """An object the reading misses is neutral at first, then keeps its last state."""
    config = _with_structure(
        mock_local_config,
        areas=[NativeObject(1, "Home"), NativeObject(2, "Other")],
        # Zone 1506 is half B of terminal 1 (firmware 5.x numbering).
        zones=[NativeZoneDef(1, "Front Door", 1, (1,)), NativeZoneDef(1506, "Back Door", 1, (2,))],
    )
    coordinator = _make_coordinator(hass, mock_config_entry, mock_local_client, config)

    await coordinator.async_refresh()
    first = coordinator.data
    assert first.areas[1] == Area(2, "Other", AreaMode.DISARMED, AreaState.READY, False)
    assert first.zones[1] == Zone(1506, "Back Door", 1, ZoneState.READY, False, False)

    area_statuses[2] = NativeAreaStatus(AreaMode.TOTAL, alarm=False, alarm_memory=True)
    zone_statuses[1506] = NativeZoneStatus(ZoneState.ALARM, excluded=True, alarm_memory=True)
    await coordinator.async_refresh()
    assert coordinator.data.areas[1].mode is AreaMode.TOTAL
    assert coordinator.data.zones[1].state is ZoneState.ALARM

    # The next reading misses them: their last known state is kept.
    del area_statuses[2]
    del zone_statuses[1506]
    await coordinator.async_refresh()
    assert coordinator.data.areas[1] == Area(2, "Other", AreaMode.TOTAL, AreaState.READY, True)
    assert coordinator.data.zones[1] == Zone(1506, "Back Door", 1, ZoneState.ALARM, True, True)


async def test_zone_carries_its_terminal(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    zone_statuses: dict[int, NativeZoneStatus],
) -> None:
    """A second-half zone reports the terminal holding it, not its own id."""
    config = _with_structure(mock_local_config, zones=[NativeZoneDef(1508, "Window", 3, (1,))])
    zone_statuses[1508] = NativeZoneStatus(ZoneState.READY, excluded=True, alarm_memory=False)
    coordinator = _make_coordinator(hass, mock_config_entry, mock_local_client, config)

    data = await coordinator._async_update_data()

    assert data.zones == [Zone(1508, "Window", 3, ZoneState.READY, False, True)]


async def test_scenarios_active_from_their_definitions(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    area_statuses: dict[int, NativeAreaStatus],
) -> None:
    """A scenario is active when every area it targets is in its target mode."""
    config = replace(
        _with_structure(
            mock_local_config,
            scenarios=[NativeObject(1, "Off"), NativeObject(2, "Away"), NativeObject(3, "Spare")],
        ),
        scenes=[SceneDef(id=1, arms={1: "disarm"}), SceneDef(id=2, arms={1: "away"})],
    )
    area_statuses[1] = NativeAreaStatus(AreaMode.TOTAL, alarm=False, alarm_memory=False)
    coordinator = _make_coordinator(hass, mock_config_entry, mock_local_client, config)
    assert coordinator.active_scene_ids() == set()  # no snapshot yet

    await coordinator.async_refresh()

    assert [(s.id, s.active) for s in coordinator.data.scenarios] == [
        (1, False),
        (2, True),
        (3, False),  # no definition: never active
    ]
    assert coordinator.active_scene_ids() == {2}


async def test_output_state_unknown_until_read(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """An output the reading does not cover has no state (None)."""
    mock_local_client.async_get_output_states.return_value = {}
    coordinator = _make_coordinator(
        hass, mock_config_entry, mock_local_client, mock_local_config
    )

    data = await coordinator._async_update_data()

    assert data.outputs == [Output(1, "Siren", None)]


async def test_no_zones_or_outputs_skips_their_reads(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """Without zones or outputs their status commands are not sent."""
    config = _with_structure(mock_local_config, zones=[], outputs=None)
    coordinator = _make_coordinator(hass, mock_config_entry, mock_local_client, config)

    data = await coordinator._async_update_data()

    assert data.zones == []
    assert data.outputs == []
    mock_local_client.async_get_zone_statuses.assert_not_awaited()
    mock_local_client.async_get_output_states.assert_not_awaited()


async def test_config_without_structure_has_no_objects(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """A config without a structure yields an empty (but valid) snapshot."""
    coordinator = _make_coordinator(
        hass, mock_config_entry, mock_local_client, replace(mock_local_config, structure=None)
    )

    data = await coordinator._async_update_data()

    assert coordinator.structure == Local6004Structure()
    assert (data.areas, data.zones, data.scenarios, data.outputs) == ([], [], [], [])


async def test_fault_and_version_from_the_system_status(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    make_system_status,
) -> None:
    """Faults, the supply voltage and the service flag come from op 0x0D."""
    mock_local_client.async_get_system_status.return_value = make_system_status(
        supply=24.3, service=True, fault_mains=True, tamper_sources={"readers": True}
    )
    coordinator = _make_coordinator(
        hass, mock_config_entry, mock_local_client, mock_local_config
    )

    data = await coordinator._async_update_data()

    assert data.fault.vcc == 24.3
    assert data.fault.has_faults is True
    assert data.fault.flags["fault_mains"] is True
    assert not data.fault.flags["low_battery"]
    assert data.panel == PanelStatus(service=True, programming=False)
    assert data.fault.tamper_sources == {"readers": True}
    assert data.power == Power(battery_voltage=13.4, battery_temperature=27, battery_resistance=115)
    assert (data.version.firmware, data.version.model_name) == ("4.07", "PX020")


# ---------------------------------------------------------------------------
# Failures: tolerated briefly, then unavailable; slower polling meanwhile
# ---------------------------------------------------------------------------
async def test_failure_without_snapshot_raises(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """With nothing to serve, a failed read raises UpdateFailed."""
    mock_local_client.async_get_area_statuses.side_effect = Local6004Error("down")
    coordinator = _make_coordinator(
        hass, mock_config_entry, mock_local_client, mock_local_config
    )

    with pytest.raises(UpdateFailed, match="down"):
        await coordinator._async_update_data()


async def _seeded(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: AsyncMock,
    config: Local6004Config,
) -> tuple[InimDataUpdateCoordinator, InimData]:
    coordinator = _make_coordinator(hass, entry, client, config)
    seed = await coordinator._async_update_data()
    coordinator.async_set_updated_data(seed)
    return coordinator, seed


async def test_transient_failures_serve_the_snapshot_then_raise(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """Failures below the threshold serve the last snapshot; then raise."""
    coordinator, seed = await _seeded(
        hass, mock_config_entry, mock_local_client, mock_local_config
    )
    mock_local_client.async_get_system_status.side_effect = Local6004Error("slow")

    for _ in range(FAILURES_BEFORE_UNAVAILABLE - 1):
        assert await coordinator._async_update_data() is seed
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    # Recovery resets the count, so the next failure is absorbed again.
    mock_local_client.async_get_system_status.side_effect = None
    fresh = await coordinator._async_update_data()
    coordinator.async_set_updated_data(fresh)
    assert coordinator._consecutive_failures == 0
    mock_local_client.async_get_system_status.side_effect = Local6004Error("slow")
    assert await coordinator._async_update_data() is fresh


async def test_slow_cycle_is_aborted_at_the_ceiling(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cycle slower than DEFAULT_CYCLE_TIMEOUT fails instead of hanging."""
    import custom_components.inim_prime.coordinator as coord_mod

    coordinator, seed = await _seeded(
        hass, mock_config_entry, mock_local_client, mock_local_config
    )
    monkeypatch.setattr(coord_mod, "DEFAULT_CYCLE_TIMEOUT", 0.05)

    async def _slow(*_args) -> dict:
        await asyncio.sleep(5)
        return {}

    mock_local_client.async_get_area_statuses.side_effect = _slow

    # With a snapshot the timeout is absorbed...
    assert await asyncio.wait_for(coordinator._async_update_data(), 2) is seed
    assert coordinator._consecutive_failures == 1
    # ...and once it persists it raises, naming the timeout.
    coordinator._consecutive_failures = FAILURES_BEFORE_UNAVAILABLE
    with pytest.raises(UpdateFailed, match="did not respond"):
        await asyncio.wait_for(coordinator._async_update_data(), 2)


async def test_backoff_slows_the_poll_then_recovers(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """After FAILURES_BEFORE_BACKOFF failures the poll slows; success restores it."""
    coordinator = _make_coordinator(
        hass, mock_config_entry, mock_local_client, mock_local_config
    )
    mock_local_client.async_get_area_statuses.side_effect = Local6004Error("down")

    for attempt in range(1, FAILURES_BEFORE_BACKOFF + 1):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        expected = (
            NATIVE_BACKOFF_INTERVAL if attempt >= FAILURES_BEFORE_BACKOFF else NATIVE_POLL_INTERVAL
        )
        assert coordinator.update_interval == timedelta(seconds=expected)

    mock_local_client.async_get_area_statuses.side_effect = None
    await coordinator._async_update_data()
    assert coordinator._consecutive_failures == 0
    assert coordinator.update_interval == timedelta(seconds=NATIVE_POLL_INTERVAL)


async def test_overlapping_refresh_coalesces(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    area_statuses: dict[int, NativeAreaStatus],
) -> None:
    """A refresh fired mid-cycle returns the snapshot, without a second read."""
    coordinator, seed = await _seeded(
        hass, mock_config_entry, mock_local_client, mock_local_config
    )
    release = asyncio.Event()
    entered = asyncio.Event()

    async def _gated() -> dict[int, NativeAreaStatus]:
        entered.set()
        await release.wait()
        return area_statuses

    mock_local_client.async_get_area_statuses.side_effect = _gated
    first = asyncio.create_task(coordinator._async_update_data())
    await entered.wait()

    assert await coordinator._async_update_data() is seed
    release.set()
    await first
    assert mock_local_client.async_get_area_statuses.await_count == 2  # seed + first


async def test_power_values_read_once_a_minute(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The power values are read on the first cycle, then every SLOW_POLL_INTERVAL."""
    clock = [1000.0]
    monkeypatch.setattr(
        "custom_components.inim_prime.coordinator.time.monotonic", lambda: clock[0]
    )
    coordinator = _make_coordinator(
        hass, mock_config_entry, mock_local_client, mock_local_config
    )
    read = mock_local_client.async_get_power_values

    await coordinator._async_update_data()
    clock[0] += SLOW_POLL_INTERVAL - 1
    read.return_value = NativePowerValues(battery_temperature=30, battery_resistance=120)
    data = await coordinator._async_update_data()
    assert read.await_count == 1
    assert data.power.battery_temperature == 27  # the last reading is kept

    clock[0] += 1
    data = await coordinator._async_update_data()
    assert read.await_count == 2
    assert (data.power.battery_temperature, data.power.battery_resistance) == (30, 120)


async def test_power_values_failure_keeps_the_cycle(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """A panel that does not answer the power values still updates everything else."""
    mock_local_client.async_get_power_values.side_effect = Local6004Error("no answer")
    coordinator = _make_coordinator(
        hass, mock_config_entry, mock_local_client, mock_local_config
    )

    data = await coordinator._async_update_data()

    assert data.power == Power(battery_voltage=13.4)
    assert coordinator._consecutive_failures == 0


def _gsm_status(result: int = 0, **faults: bool) -> NativeGsmStatus:
    return NativeGsmStatus(
        result=result, faults=faults, networks=("4G",), signal=55, operator_code="00101"
    )


async def test_gsm_status_read_every_five_minutes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    make_system_status,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GSM status is read on the first cycle, then every GSM_POLL_INTERVAL."""
    clock = [1000.0]
    monkeypatch.setattr(
        "custom_components.inim_prime.coordinator.time.monotonic", lambda: clock[0]
    )
    mock_local_client.async_get_gsm_status.return_value = _gsm_status(roaming=True)
    mock_local_client.async_get_system_status.return_value = replace(
        make_system_status(), volte=True
    )
    coordinator = _make_coordinator(
        hass, mock_config_entry, mock_local_client, mock_local_config
    )
    read = mock_local_client.async_get_gsm_status

    data = await coordinator._async_update_data()
    assert data.gsm == Gsm(
        state="ok",
        signal=55,
        networks=("4G",),
        operator_code="00101",
        volte=True,
        faults={"roaming": True},
    )
    clock[0] += GSM_POLL_INTERVAL - 1
    await coordinator._async_update_data()
    assert read.await_count == 1
    clock[0] += 1
    await coordinator._async_update_data()
    assert read.await_count == 2


@pytest.mark.parametrize(
    ("result", "state"), [(GSM_NOT_INITIALISED, "not_initialised"), (9, "error")]
)
async def test_gsm_status_not_ok_has_no_signal(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    result: int,
    state: str,
) -> None:
    """A module that is not ready reports its state, without a signal level."""
    mock_local_client.async_get_gsm_status.return_value = _gsm_status(result)
    coordinator = _make_coordinator(
        hass, mock_config_entry, mock_local_client, mock_local_config
    )

    data = await coordinator._async_update_data()

    assert data.gsm is not None
    assert (data.gsm.state, data.gsm.signal) == (state, None)


async def test_gsm_status_without_module_or_answer(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """No GSM module, or no answer at all, leaves the GSM data out."""
    mock_local_client.async_get_gsm_status.return_value = _gsm_status(GSM_NO_MODULE)
    coordinator = _make_coordinator(
        hass, mock_config_entry, mock_local_client, mock_local_config
    )
    assert (await coordinator._async_update_data()).gsm is None

    mock_local_client.async_get_gsm_status.side_effect = Local6004Error("no answer")
    coordinator = _make_coordinator(
        hass, mock_config_entry, mock_local_client, mock_local_config
    )
    data = await coordinator._async_update_data()
    assert data.gsm is None
    assert coordinator._consecutive_failures == 0
