"""Tests for the panel write commands (native TCP 6004 only)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime import commands
from custom_components.inim_prime.client import (
    SCENE_DECODED_PARTITIONS,
    AreaMode,
    ArmMode,
    Local6004Config,
    Local6004Error,
    NativeCommandInvalid,
    NativeCommandNotSent,
    NativeCommandRejected,
    NativeCommandUncertain,
    NativeZoneDef,
    NativeZonesNotReady,
    NativeZoneStatus,
    SceneDef,
    ZoneState,
    local6004,
)
from custom_components.inim_prime.client.local6004 import (
    ZONE_OPT_AUTO_BYPASS,
    ZONE_OPT_CHIME,
    ZONE_OPT_INTERIOR,
)
from custom_components.inim_prime.coordinator import InimDataUpdateCoordinator
from tests.test_local6004 import _decrypt, _FakeReader, _FakeWriter, _resp, _status_resp

# conftest: one area (id 1, disarmed), one zone (id 1, ready) in area 1,
# scenario 1, output 1.


async def _coordinator(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: AsyncMock,
    config: Local6004Config,
) -> InimDataUpdateCoordinator:
    entry.add_to_hass(hass)
    coordinator = InimDataUpdateCoordinator(hass, entry, client, config)
    coordinator.async_set_updated_data(await coordinator._async_update_data())
    return coordinator


def _with_zones(config: Local6004Config, *zones: NativeZoneDef) -> Local6004Config:
    assert config.structure is not None
    return replace(config, structure=replace(config.structure, zones=list(zones)))


# --------------------------------------------------------------- commands
async def test_commands_are_sent_natively_and_refresh(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, mock_local_config)
    reads = mock_local_client.async_get_area_statuses.await_count

    await commands.async_arm_area(coordinator, 1, ArmMode.TOTAL)
    # Zone 1 (area 1) must be read ready live, right before sending.
    mock_local_client.async_set_area_modes.assert_awaited_once_with(
        {1: AreaMode.TOTAL}, require_ready={1}
    )
    # The state is re-read right away.
    assert mock_local_client.async_get_area_statuses.await_count == reads + 1

    await commands.async_arm_area(coordinator, 1, ArmMode.PARTIAL)
    await commands.async_arm_area(coordinator, 1, ArmMode.SNAPSHOT)
    await commands.async_arm_area(coordinator, 1, ArmMode.DISARM)
    assert [
        (c.args[0], c.kwargs["require_ready"])
        for c in mock_local_client.async_set_area_modes.await_args_list[1:]
    ] == [
        ({1: AreaMode.PARTIAL}, {1}),
        ({1: AreaMode.SNAPSHOT}, {1}),
        ({1: AreaMode.DISARMED}, set()),  # disarming checks nothing
    ]
    await commands.async_clear_alarm_memory(coordinator, 1)
    mock_local_client.async_reset_areas.assert_awaited_once_with([1])
    await commands.async_set_zone_excluded(coordinator, 1, False)
    mock_local_client.async_set_zone_bypass.assert_awaited_once_with(1, False)
    await commands.async_set_output(coordinator, 1505, 1)
    await commands.async_set_output(coordinator, 1506, 0)
    assert [c.args for c in mock_local_client.async_set_output.await_args_list] == [
        (1505, True),
        (1506, False),
    ]
    assert mock_local_client.async_get_area_statuses.await_count == reads + 8


@pytest.mark.parametrize(
    "call",
    [
        lambda c: commands.async_arm_area(c, 1, ArmMode.TOTAL),
        lambda c: commands.async_apply_scenario(c, 1),
        lambda c: commands.async_clear_alarm_memory(c, 1),
        lambda c: commands.async_set_zone_excluded(c, 1, True),
        lambda c: commands.async_set_output(c, 1, 1),
    ],
)
async def test_unverified_firmware_refuses_every_command(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    call,  # noqa: ANN001
) -> None:
    mock_local_client.supports_native_writes = False
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, mock_local_config)
    with pytest.raises(HomeAssistantError) as info:
        await call(coordinator)
    assert info.value.translation_key == "command_failed"
    for write in (
        mock_local_client.async_set_area_modes,
        mock_local_client.async_reset_areas,
        mock_local_client.async_set_zone_bypass,
        mock_local_client.async_set_output,
    ):
        write.assert_not_awaited()


# ------------------------------------------------------- arming readiness gate
def _zone(zone_id: int, areas: tuple[int, ...], options: int | None = None) -> NativeZoneDef:
    return NativeZoneDef(zone_id, f"Zone {zone_id}", zone_id, areas, options=options)


@pytest.mark.parametrize(
    ("modes", "expected"),
    [
        ({1: AreaMode.TOTAL}, {1, 2, 4, 5}),
        ({1: AreaMode.DISARMED}, set()),  # disarming checks nothing
        ({2: AreaMode.TOTAL}, {3, 4}),
        # Interior zones are left out of partial and snapshot arming...
        ({1: AreaMode.PARTIAL}, {1, 2, 5}),
        ({1: AreaMode.SNAPSHOT}, {1, 2, 5}),
        # ...unless one of their areas goes to total.
        ({1: AreaMode.PARTIAL, 2: AreaMode.TOTAL}, {1, 2, 3, 4, 5}),
        ({1: AreaMode.PARTIAL, 2: AreaMode.SNAPSHOT}, {1, 2, 3, 5}),
        ({7: AreaMode.TOTAL}, set()),  # no zone in that area
    ],
)
def test_zones_to_check(modes: dict[int, AreaMode], expected: set[int]) -> None:
    zones = [
        _zone(1, (1,)),
        _zone(2, (1,), options=0),
        _zone(3, (2,)),
        _zone(4, (1, 2), options=ZONE_OPT_INTERIOR),
        _zone(5, (1,), options=ZONE_OPT_CHIME),
        _zone(6, (1, 2), options=ZONE_OPT_AUTO_BYPASS),  # the panel bypasses it
        _zone(7, (3,), options=ZONE_OPT_AUTO_BYPASS | ZONE_OPT_INTERIOR),
    ]
    assert commands.zones_to_check(zones, modes) == expected


async def test_arm_checks_the_zones_the_panel_would(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    config = _with_zones(
        mock_local_config,
        _zone(1, (1,)),
        _zone(2, (1,), options=ZONE_OPT_INTERIOR),
        _zone(3, (1,), options=ZONE_OPT_AUTO_BYPASS),
        _zone(4, (2,)),
    )
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, config)

    await commands.async_arm_area(coordinator, 1, ArmMode.TOTAL)
    await commands.async_arm_area(coordinator, 1, ArmMode.PARTIAL)

    calls = mock_local_client.async_set_area_modes.await_args_list
    assert [c.kwargs["require_ready"] for c in calls] == [{1, 2}, {1}]


async def test_zones_not_ready_live_is_reported(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, mock_local_config)
    reads = mock_local_client.async_get_area_statuses.await_count
    mock_local_client.async_set_area_modes.side_effect = NativeZonesNotReady(
        3, "zones not ready: [1]"
    )

    with pytest.raises(HomeAssistantError) as info:
        await commands.async_arm_area(coordinator, 1, ArmMode.TOTAL)

    assert info.value.translation_key == "zones_not_ready"
    mock_local_client.async_set_area_modes.assert_awaited_once()
    # Nothing was sent: no re-read needed.
    assert mock_local_client.async_get_area_statuses.await_count == reads


async def test_unknown_area_is_refused(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, mock_local_config)
    with pytest.raises(HomeAssistantError) as info:
        await commands.async_arm_area(coordinator, 7, ArmMode.DISARM)
    assert info.value.translation_key == "command_failed"
    assert info.value.translation_placeholders == {"error": "unknown area [7]"}
    mock_local_client.async_set_area_modes.assert_not_awaited()


# --------------------------------------------------------------- scenarios
@pytest.mark.parametrize(
    ("scenes", "modes"),
    [
        ([SceneDef(id=1, arms={1: "away"})], {1: AreaMode.TOTAL}),
        ([SceneDef(id=1, arms={1: "stay"})], {1: AreaMode.PARTIAL}),
        ([SceneDef(id=1, arms={1: "disarm"})], {1: AreaMode.DISARMED}),
    ],
)
async def test_scenario_is_one_native_arming(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    scenes: list[SceneDef],
    modes: dict[int, AreaMode],
) -> None:
    config = replace(mock_local_config, scenes=scenes)
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, config)

    await commands.async_apply_scenario(coordinator, 1)

    mock_local_client.async_set_area_modes.assert_awaited_once()
    assert mock_local_client.async_set_area_modes.await_args.args == (modes,)
    assert commands.scenario_modes(coordinator, 1) == modes


@pytest.mark.parametrize(
    "scenes",
    [
        [SceneDef(id=1, arms={1: "away+stay"})],  # no single mode
        [SceneDef(id=1, arms={1: "0x8"})],  # unknown mode
        [SceneDef(id=2, arms={1: "away"})],  # scenario 1 not defined
    ],
)
async def test_scenario_without_a_single_mode_per_area_is_refused(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    scenes: list[SceneDef],
) -> None:
    config = replace(mock_local_config, scenes=scenes)
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, config)

    with pytest.raises(HomeAssistantError) as info:
        await commands.async_apply_scenario(coordinator, 1)

    assert info.value.translation_key == "scenario_not_supported"
    mock_local_client.async_set_area_modes.assert_not_awaited()


async def test_scenario_on_a_panel_beyond_the_decoded_partitions_is_refused(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """Scenes decode partitions 0-11 only; an area 12+ may be a hidden target."""
    config = replace(mock_local_config, scenes=[SceneDef(id=1, arms={1: "away"})])
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, config)
    extra = replace(coordinator.data.areas[0], id=SCENE_DECODED_PARTITIONS, label="Far")
    coordinator.data = replace(coordinator.data, areas=[*coordinator.data.areas, extra])

    with pytest.raises(HomeAssistantError) as info:
        await commands.async_apply_scenario(coordinator, 1)

    assert info.value.translation_key == "scenario_not_supported"
    mock_local_client.async_set_area_modes.assert_not_awaited()


async def test_scenario_setting_an_unknown_area_is_refused(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    config = replace(mock_local_config, scenes=[SceneDef(id=1, arms={1: "away", 4: "away"})])
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, config)

    with pytest.raises(HomeAssistantError) as info:
        await commands.async_apply_scenario(coordinator, 1)

    assert info.value.translation_key == "command_failed"
    mock_local_client.async_set_area_modes.assert_not_awaited()


# ------------------------------------------------------------ failure safety
_COMMANDS = [
    (lambda c: commands.async_arm_area(c, 1, ArmMode.TOTAL), "async_set_area_modes"),
    (lambda c: commands.async_arm_area(c, 1, ArmMode.DISARM), "async_set_area_modes"),
    (lambda c: commands.async_apply_scenario(c, 1), "async_set_area_modes"),
    (lambda c: commands.async_clear_alarm_memory(c, 1), "async_reset_areas"),
    (lambda c: commands.async_set_zone_excluded(c, 1, True), "async_set_zone_bypass"),
    (lambda c: commands.async_set_output(c, 1, 1), "async_set_output"),
]


@pytest.mark.parametrize(("call", "native_write"), _COMMANDS)
@pytest.mark.parametrize(
    "error", [NativeCommandNotSent(3, "refused"), NativeCommandInvalid(3, "out of range")]
)
async def test_never_sent_is_command_failed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    caplog: pytest.LogCaptureFixture,
    call,  # noqa: ANN001
    native_write: str,
    error: Exception,
) -> None:
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, mock_local_config)
    getattr(mock_local_client, native_write).side_effect = error

    with pytest.raises(HomeAssistantError) as info:
        await call(coordinator)

    assert info.value.translation_key == "command_failed"
    assert info.value.translation_placeholders == {"error": str(error)}
    getattr(mock_local_client, native_write).assert_awaited_once()
    assert "was not sent" in caplog.text


@pytest.mark.parametrize(("call", "native_write"), _COMMANDS)
@pytest.mark.parametrize(
    "error", [NativeCommandUncertain(3, "TimeoutError"), NativeCommandRejected(3, "bad echo")]
)
async def test_possibly_sent_is_never_retried(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    call,  # noqa: ANN001
    native_write: str,
    error: Exception,
) -> None:
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, mock_local_config)
    reads = mock_local_client.async_get_area_statuses.await_count
    getattr(mock_local_client, native_write).side_effect = error

    with pytest.raises(HomeAssistantError) as info:
        await call(coordinator)

    assert info.value.translation_key == "native_command_uncertain"
    getattr(mock_local_client, native_write).assert_awaited_once()
    # The real state is re-read so the UI shows what the panel did.
    assert mock_local_client.async_get_area_statuses.await_count == reads + 1


async def test_a_plain_value_error_propagates(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """Only the client's own errors are translated."""
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, mock_local_config)
    mock_local_client.async_set_output.side_effect = ValueError("unexpected")
    with pytest.raises(ValueError):
        await commands.async_set_output(coordinator, 1, 1)


# ------------------------------------------------- native bypass read-back
def _zone_status(excluded: bool) -> dict[int, NativeZoneStatus]:
    return {1: NativeZoneStatus(state=ZoneState.READY, excluded=excluded, alarm_memory=False)}


@pytest.mark.parametrize(
    "reads",
    [
        [_zone_status(True)],  # applied at once
        [_zone_status(False), _zone_status(True)],  # shows up on the next read
        [{}],  # zone not reported: nothing to tell
        [Local6004Error("down")],  # read failed: nothing to tell
    ],
)
async def test_bypass_read_back_accepts(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    monkeypatch: pytest.MonkeyPatch,
    reads: list,
) -> None:
    monkeypatch.setattr(commands, "_BYPASS_CHECK_DELAY", 0)
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, mock_local_config)
    mock_local_client.async_get_zone_statuses.side_effect = [*reads, _zone_status(True)]

    await commands.async_set_zone_excluded(coordinator, 1, True)

    mock_local_client.async_set_zone_bypass.assert_awaited_once_with(1, True)


@pytest.mark.parametrize(("zone_id", "label"), [(1, "Front Door"), (9, "9")])
async def test_bypass_never_applied_is_reported(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    monkeypatch: pytest.MonkeyPatch,
    zone_id: int,
    label: str,
) -> None:
    """The panel acknowledged but the zone never shows bypassed: a clear error."""
    monkeypatch.setattr(commands, "_BYPASS_CHECK_DELAY", 0)
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, mock_local_config)
    mock_local_client.async_get_zone_statuses.return_value = {
        zone_id: NativeZoneStatus(state=ZoneState.READY, excluded=False, alarm_memory=False)
    }
    reads = mock_local_client.async_get_area_statuses.await_count

    with pytest.raises(HomeAssistantError) as err:
        await commands.async_set_zone_excluded(coordinator, zone_id, True)

    assert err.value.translation_key == "bypass_not_applied"
    assert err.value.translation_placeholders == {"zone": label}
    assert mock_local_client.async_get_zone_statuses.await_count >= commands._BYPASS_CHECKS
    mock_local_client.async_set_zone_bypass.assert_awaited_once()  # never retried
    assert mock_local_client.async_get_area_statuses.await_count == reads + 1


# ------------------------------------------------------- through the entities
async def test_entities_send_natively(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
) -> None:
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_arm_away",
        {"entity_id": "alarm_control_panel.inim_prime_home"},
        blocking=True,
    )
    patch_client.async_set_area_modes.assert_awaited_once_with(
        {1: AreaMode.TOTAL}, require_ready={1}
    )

    patch_client.async_set_area_modes.side_effect = NativeCommandUncertain(3, "lost")
    with pytest.raises(HomeAssistantError) as info:
        await hass.services.async_call(
            "alarm_control_panel",
            "alarm_disarm",
            {"entity_id": "alarm_control_panel.inim_prime_home"},
            blocking=True,
        )
    assert info.value.translation_key == "native_command_uncertain"
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)


# ------------------------------------- real client on fake streams (no panel)
def _ops(writer: _FakeWriter) -> list[int]:
    return [int.from_bytes(_decrypt(frame)[:4], "little") for frame in writer.sent]


async def test_garbage_answer_after_sending_is_never_retried(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad-preamble answer means the write left: uncertain, sent once."""
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, mock_local_config)
    writer = _FakeWriter()
    garbage = b"\xaa\xaa" + _resp(bytes(18))[2:]

    async def fake_open(host: str, port: int) -> tuple[_FakeReader, _FakeWriter]:
        return _FakeReader(garbage), writer

    monkeypatch.setattr(local6004.asyncio, "open_connection", fake_open)
    coordinator.client = local6004.Local6004Client("host", "pass")

    with pytest.raises(HomeAssistantError) as info:
        await commands.async_set_zone_excluded(coordinator, 1, True)

    assert info.value.translation_key == "native_command_uncertain"
    # One bypass frame; the refresh after it only reads.
    assert _ops(writer).count(9) == 1


async def test_hung_liveness_read_is_never_sent(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kept connection stalls, the reconnect fails: nothing written, command_failed."""
    coordinator = await _coordinator(hass, mock_config_entry, mock_local_client, mock_local_config)

    class _StallsAfterFirst(_FakeReader):
        async def readexactly(self, n: int) -> bytes:
            if self._pos >= len(self._buf):
                await asyncio.sleep(5)
            return await super().readexactly(n)

    stalled_writer = _FakeWriter()
    opened: list[str] = []

    async def fake_open(host: str, port: int) -> tuple[_FakeReader, _FakeWriter]:
        opened.append(host)
        if len(opened) > 1:
            raise OSError("refused")
        return _StallsAfterFirst(_status_resp({})), stalled_writer

    monkeypatch.setattr(local6004.asyncio, "open_connection", fake_open)
    monkeypatch.setattr(local6004, "STATUS_TIMEOUT", 0.05)
    client = local6004.Local6004Client("host", "pass")
    await client.async_get_area_statuses()  # opens the kept connection
    coordinator.client = client

    with pytest.raises(HomeAssistantError) as info:
        await commands.async_set_output(coordinator, 1005, 1)

    assert info.value.translation_key == "command_failed"
    # Only read-only status reads went out: the first read and the probe.
    assert _ops(stalled_writer) == [6, 6]
    assert len(opened) == 2
