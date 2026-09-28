"""Tests for the opt-in native write commands and their cgi fallback rules."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime import commands
from custom_components.inim_prime.client import (
    SCENE_DECODED_PARTITIONS,
    ApiStatus,
    AreaMode,
    ArmMode,
    InimApiError,
    InimConnectionError,
    Local6004Config,
    NativeCommandInvalid,
    NativeCommandNotSent,
    NativeCommandRejected,
    NativeCommandUncertain,
    NativeZonesNotReady,
    SceneDef,
    ZoneState,
    local6004,
)
from custom_components.inim_prime.const import (
    CONF_NATIVE_AREA_POLL,
    CONF_NATIVE_COMMANDS,
)
from custom_components.inim_prime.coordinator import InimDataUpdateCoordinator
from tests.test_local6004 import _FakeReader, _FakeWriter, _resp, _status_resp

# conftest: one area (id 1, disarmed), one zone (id 1, ready) in area 1.


def _native() -> AsyncMock:
    native = AsyncMock()
    native.async_get_area_statuses.return_value = {}
    native.async_get_zone_statuses.return_value = {}
    return native


async def _coordinator(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    cgi: AsyncMock,
    local_config: Local6004Config,
    *,
    commands_on: bool = True,
    poll: bool = True,
) -> tuple[InimDataUpdateCoordinator, AsyncMock]:
    entry.add_to_hass(hass)
    coordinator = InimDataUpdateCoordinator(hass, entry, cgi)
    coordinator.local_config = local_config
    coordinator.async_set_updated_data(await coordinator._async_update_data())
    native = _native()
    if poll:
        coordinator.async_attach_native(native)
    if commands_on:
        coordinator.command_client = native
    return coordinator, native


def _set_zone(coordinator: InimDataUpdateCoordinator, **changes: object) -> None:
    zone = replace(coordinator.data.zones[0], **changes)  # type: ignore[arg-type]
    coordinator.data = replace(coordinator.data, zones=[zone])


# --------------------------------------------------------------- option off
async def test_option_off_uses_the_cgi_only(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    coordinator, native = await _coordinator(
        hass, mock_config_entry, mock_client, mock_local_config, commands_on=False
    )
    await commands.async_arm_area(coordinator, 1, ArmMode.TOTAL)
    await commands.async_arm_area(coordinator, 1, ArmMode.DISARM)
    await commands.async_apply_scenario(coordinator, 1)
    await commands.async_clear_alarm_memory(coordinator, 1)
    await commands.async_set_zone_excluded(coordinator, 1, True)
    await commands.async_set_output(coordinator, 1005, 1)

    mock_client.arm_area.assert_awaited_once_with(1, ArmMode.TOTAL)
    mock_client.disarm_area.assert_awaited_once_with(1)
    mock_client.apply_scenario.assert_awaited_once_with(1)
    mock_client.clear_alarm_memory.assert_awaited_once_with(1)
    mock_client.set_zone_excluded.assert_awaited_once_with(1, True)
    mock_client.set_output.assert_awaited_once_with(1005, 1)
    for write in (
        native.async_set_area_modes,
        native.async_set_zone_bypass,
        native.async_set_output,
        native.async_reset_areas,
    ):
        write.assert_not_awaited()


# ---------------------------------------------------------------- option on
async def test_option_on_sends_natively_and_refreshes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    coordinator, native = await _coordinator(
        hass, mock_config_entry, mock_client, mock_local_config
    )
    await commands.async_arm_area(coordinator, 1, ArmMode.TOTAL)
    # Zone 1 (area 1) must be read ready live, right before sending.
    native.async_set_area_modes.assert_awaited_once_with({1: AreaMode.TOTAL}, require_ready={1})
    # The state is re-read natively right away.
    native.async_get_area_statuses.assert_awaited_once()

    await commands.async_arm_area(coordinator, 1, ArmMode.PARTIAL)
    await commands.async_arm_area(coordinator, 1, ArmMode.SNAPSHOT)
    await commands.async_arm_area(coordinator, 1, ArmMode.DISARM)
    assert [
        (c.args[0], c.kwargs["require_ready"])
        for c in native.async_set_area_modes.await_args_list[1:]
    ] == [
        ({1: AreaMode.PARTIAL}, {1}),
        ({1: AreaMode.SNAPSHOT}, {1}),
        ({1: AreaMode.DISARMED}, set()),  # disarming checks nothing
    ]
    await commands.async_clear_alarm_memory(coordinator, 1)
    native.async_reset_areas.assert_awaited_once_with([1])
    await commands.async_set_zone_excluded(coordinator, 1, False)
    native.async_set_zone_bypass.assert_awaited_once_with(1, False)
    await commands.async_set_output(coordinator, 1005, 1)
    await commands.async_set_output(coordinator, 1006, 0)
    assert [c.args for c in native.async_set_output.await_args_list] == [
        (1005, True),
        (1006, False),
    ]
    for write in (
        mock_client.arm_area,
        mock_client.disarm_area,
        mock_client.clear_alarm_memory,
        mock_client.set_zone_excluded,
        mock_client.set_output,
    ):
        write.assert_not_awaited()


# ------------------------------------------------------- arming readiness gate
@pytest.mark.parametrize(
    ("zone_areas", "require_ready"),
    [
        ({1: [1]}, {1}),
        ({1: [2]}, set()),  # the only zone is in another area
        ({1: [1, 2], 5: [1], 6: [3]}, {1, 5}),  # every zone of the area, known or not
        ({9: [1]}, None),  # zone 1's areas unknown -> cgi
        ({}, None),  # no zone -> area map at all -> cgi
    ],
)
async def test_arm_checks_the_target_zones_live(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    mock_local_config: Local6004Config,
    zone_areas: dict[int, list[int]],
    require_ready: set[int] | None,
) -> None:
    config = replace(mock_local_config, zone_areas=zone_areas)
    coordinator, native = await _coordinator(hass, mock_config_entry, mock_client, config)
    # The cached state is not trusted: an open zone here changes nothing.
    _set_zone(coordinator, state=ZoneState.ALARM)

    await commands.async_arm_area(coordinator, 1, ArmMode.TOTAL)

    if require_ready is None:
        native.async_set_area_modes.assert_not_awaited()
        mock_client.arm_area.assert_awaited_once_with(1, ArmMode.TOTAL)
    else:
        native.async_set_area_modes.assert_awaited_once_with(
            {1: AreaMode.TOTAL}, require_ready=require_ready
        )
        mock_client.arm_area.assert_not_awaited()


async def test_zones_not_ready_live_goes_to_the_cgi(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """The panel's own check (ZONES_NOT_READY) then decides, as with the option off."""
    coordinator, native = await _coordinator(
        hass, mock_config_entry, mock_client, mock_local_config
    )
    native.async_set_area_modes.side_effect = NativeZonesNotReady(3, "zones not ready: [1]")
    mock_client.arm_area.side_effect = InimApiError(ApiStatus.ZONES_NOT_READY)

    with pytest.raises(InimApiError):
        await commands.async_arm_area(coordinator, 1, ArmMode.TOTAL)
    native.async_set_area_modes.assert_awaited_once()
    mock_client.arm_area.assert_awaited_once_with(1, ArmMode.TOTAL)

    mock_client.arm_area.side_effect = None  # zones closed meanwhile: the cgi arms
    await commands.async_arm_area(coordinator, 1, ArmMode.TOTAL)
    assert mock_client.arm_area.await_count == 2


async def test_arm_works_without_the_native_poll(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """The live pre-check does not depend on the poll's cached state."""
    coordinator, native = await _coordinator(
        hass, mock_config_entry, mock_client, mock_local_config, poll=False
    )
    await commands.async_arm_area(coordinator, 1, ArmMode.TOTAL)
    await commands.async_arm_area(coordinator, 1, ArmMode.DISARM)
    assert [c.args[0] for c in native.async_set_area_modes.await_args_list] == [
        {1: AreaMode.TOTAL},
        {1: AreaMode.DISARMED},
    ]
    mock_client.arm_area.assert_not_awaited()
    mock_client.disarm_area.assert_not_awaited()


async def test_arm_without_local_config_or_unknown_area_uses_cgi(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    coordinator, native = await _coordinator(
        hass, mock_config_entry, mock_client, mock_local_config
    )
    await commands.async_arm_area(coordinator, 7, ArmMode.DISARM)  # not a known area
    mock_client.disarm_area.assert_awaited_once_with(7)
    coordinator.local_config = None
    await commands.async_arm_area(coordinator, 1, ArmMode.TOTAL)
    mock_client.arm_area.assert_awaited_once_with(1, ArmMode.TOTAL)
    native.async_set_area_modes.assert_not_awaited()


# --------------------------------------------------------------- scenarios
@pytest.mark.parametrize(
    ("scenes", "native_modes"),
    [
        ([SceneDef(id=1, arms={1: "away"})], {1: AreaMode.TOTAL}),
        ([SceneDef(id=1, arms={1: "stay"})], {1: AreaMode.PARTIAL}),
        ([SceneDef(id=1, arms={1: "disarm"})], {1: AreaMode.DISARMED}),
        ([SceneDef(id=1, arms={1: "away+stay"})], None),  # no single mode
        ([SceneDef(id=1, arms={1: "0x8"})], None),
        ([SceneDef(id=1, arms={1: "away", 4: "away"})], None),  # area 4 unknown
        ([SceneDef(id=2, arms={1: "away"})], None),  # scenario 1 not defined
    ],
)
async def test_scenario_routing(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    mock_local_config: Local6004Config,
    scenes: list[SceneDef],
    native_modes: dict[int, AreaMode] | None,
) -> None:
    config = replace(mock_local_config, scenes=scenes)
    coordinator, native = await _coordinator(hass, mock_config_entry, mock_client, config)

    await commands.async_apply_scenario(coordinator, 1)

    if native_modes is None:
        mock_client.apply_scenario.assert_awaited_once_with(1)
        native.async_set_area_modes.assert_not_awaited()
    else:
        native.async_set_area_modes.assert_awaited_once()
        assert native.async_set_area_modes.await_args.args == (native_modes,)
        mock_client.apply_scenario.assert_not_awaited()


async def test_scenario_on_a_panel_beyond_the_decoded_partitions_uses_cgi(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """Scenes decode partitions 0-11 only; an area 12+ may be a hidden target."""
    config = replace(mock_local_config, scenes=[SceneDef(id=1, arms={1: "away"})])
    coordinator, native = await _coordinator(hass, mock_config_entry, mock_client, config)
    extra = replace(coordinator.data.areas[0], id=SCENE_DECODED_PARTITIONS, label="Far")
    coordinator.data = replace(coordinator.data, areas=[*coordinator.data.areas, extra])

    await commands.async_apply_scenario(coordinator, 1)

    mock_client.apply_scenario.assert_awaited_once_with(1)
    native.async_set_area_modes.assert_not_awaited()


async def test_scenario_without_local_config_uses_cgi(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    coordinator, native = await _coordinator(
        hass, mock_config_entry, mock_client, mock_local_config
    )
    coordinator.local_config = None
    await commands.async_apply_scenario(coordinator, 1)
    mock_client.apply_scenario.assert_awaited_once_with(1)
    native.async_set_area_modes.assert_not_awaited()


# ------------------------------------------------------------ fallback safety
@pytest.mark.parametrize(
    "error", [NativeCommandNotSent(3, "refused"), NativeCommandInvalid(3, "out of range")]
)
async def test_never_sent_falls_back_to_the_cgi_once(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    mock_local_config: Local6004Config,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    coordinator, native = await _coordinator(
        hass, mock_config_entry, mock_client, mock_local_config
    )
    native.async_set_area_modes.side_effect = error

    await commands.async_arm_area(coordinator, 1, ArmMode.TOTAL)

    native.async_set_area_modes.assert_awaited_once()
    mock_client.arm_area.assert_awaited_once_with(1, ArmMode.TOTAL)
    assert "was not sent" in caplog.text


async def test_a_plain_value_error_is_not_a_fallback(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """Only the client's explicit "never sent" errors may reach the cgi."""
    coordinator, native = await _coordinator(
        hass, mock_config_entry, mock_client, mock_local_config
    )
    native.async_set_output.side_effect = ValueError("unexpected")
    with pytest.raises(ValueError):
        await commands.async_set_output(coordinator, 1005, 1)
    mock_client.set_output.assert_not_awaited()


async def test_cgi_failure_after_fallback_propagates(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    coordinator, native = await _coordinator(
        hass, mock_config_entry, mock_client, mock_local_config
    )
    native.async_set_zone_bypass.side_effect = NativeCommandNotSent(9, "refused")
    mock_client.set_zone_excluded.side_effect = InimConnectionError("down")
    with pytest.raises(InimConnectionError):
        await commands.async_set_zone_excluded(coordinator, 1, True)


@pytest.mark.parametrize(
    ("call", "native_write", "cgi_write"),
    [
        (
            lambda c: commands.async_arm_area(c, 1, ArmMode.TOTAL),
            "async_set_area_modes",
            "arm_area",
        ),
        (
            lambda c: commands.async_arm_area(c, 1, ArmMode.DISARM),
            "async_set_area_modes",
            "disarm_area",
        ),
        (
            lambda c: commands.async_apply_scenario(c, 1),
            "async_set_area_modes",
            "apply_scenario",
        ),
        (
            lambda c: commands.async_clear_alarm_memory(c, 1),
            "async_reset_areas",
            "clear_alarm_memory",
        ),
        (
            lambda c: commands.async_set_zone_excluded(c, 1, True),
            "async_set_zone_bypass",
            "set_zone_excluded",
        ),
        (lambda c: commands.async_set_output(c, 1005, 1), "async_set_output", "set_output"),
    ],
)
@pytest.mark.parametrize(
    "error", [NativeCommandUncertain(3, "TimeoutError"), NativeCommandRejected(3, "bad echo")]
)
async def test_possibly_sent_is_never_retried_on_the_cgi(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    mock_local_config: Local6004Config,
    call,  # noqa: ANN001
    native_write: str,
    cgi_write: str,
    error: Exception,
) -> None:
    coordinator, native = await _coordinator(
        hass, mock_config_entry, mock_client, mock_local_config
    )
    getattr(native, native_write).side_effect = error

    with pytest.raises(HomeAssistantError) as info:
        await call(coordinator)

    assert info.value.translation_key == "native_command_uncertain"
    getattr(native, native_write).assert_awaited_once()
    getattr(mock_client, cgi_write).assert_not_awaited()
    # The real state is re-read so the UI shows what the panel did.
    native.async_get_area_statuses.assert_awaited_once()
    assert mock_client.get_areas.await_count == 2  # setup cycle + the refresh


# ------------------------------------------------------------ setup / entities
async def test_setup_option_attaches_the_command_client(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    mock_local_client: AsyncMock,
) -> None:
    """Commands on with the poll off: the shared connection is still closed on unload."""
    caplog.set_level(logging.INFO)
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={CONF_NATIVE_AREA_POLL: False, CONF_NATIVE_COMMANDS: True},
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_config_entry.runtime_data.coordinator
    assert coordinator.command_client is mock_local_client
    assert coordinator.native_client is None
    assert "only refreshed by the slower cgi poll" in caplog.text
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    mock_local_client.async_close.assert_awaited_once()


async def test_setup_default_keeps_commands_on_the_cgi(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    mock_local_client: AsyncMock,
) -> None:
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.runtime_data.coordinator.command_client is None

    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_arm_away",
        {"entity_id": "alarm_control_panel.inim_prime_home"},
        blocking=True,
    )
    patch_client.arm_area.assert_awaited_once_with(1, ArmMode.TOTAL)
    mock_local_client.async_set_area_modes.assert_not_awaited()


async def test_entities_route_natively_with_the_option_on(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    mock_local_client: AsyncMock,
) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_NATIVE_COMMANDS: True}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_arm_away",
        {"entity_id": "alarm_control_panel.inim_prime_home"},
        blocking=True,
    )
    mock_local_client.async_set_area_modes.assert_awaited_once_with(
        {1: AreaMode.TOTAL}, require_ready={1}
    )
    patch_client.arm_area.assert_not_awaited()

    mock_local_client.async_set_area_modes.side_effect = NativeCommandUncertain(3, "lost")
    with pytest.raises(HomeAssistantError, match="may have reached the panel"):
        await hass.services.async_call(
            "alarm_control_panel",
            "alarm_disarm",
            {"entity_id": "alarm_control_panel.inim_prime_home"},
            blocking=True,
        )
    patch_client.disarm_area.assert_not_awaited()


# ------------------------------------- real client on fake streams (no panel)
async def test_garbage_answer_after_sending_is_never_retried(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    mock_local_config: Local6004Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad-preamble answer means the write left: error, and no cgi call."""
    coordinator, _ = await _coordinator(
        hass, mock_config_entry, mock_client, mock_local_config, poll=False
    )
    writer = _FakeWriter()
    garbage = b"\xaa\xaa" + _resp(bytes(18))[2:]

    async def fake_open(host: str, port: int) -> tuple[_FakeReader, _FakeWriter]:
        return _FakeReader(garbage), writer

    monkeypatch.setattr(local6004.asyncio, "open_connection", fake_open)
    coordinator.command_client = local6004.Local6004Client("host", "pass")

    with pytest.raises(HomeAssistantError) as info:
        await commands.async_set_zone_excluded(coordinator, 1, True)

    assert info.value.translation_key == "native_command_uncertain"
    assert len(writer.sent) == 1
    mock_client.set_zone_excluded.assert_not_awaited()


async def test_hung_liveness_read_falls_back_to_the_cgi_once(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    mock_local_config: Local6004Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kept connection stalls, the reconnect fails: nothing left, one cgi call."""
    coordinator, _ = await _coordinator(
        hass, mock_config_entry, mock_client, mock_local_config, poll=False
    )

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
    coordinator.command_client = client

    await commands.async_set_output(coordinator, 1005, 1)

    mock_client.set_output.assert_awaited_once_with(1005, 1)
    # Only read-only status reads went out: the first read and the probe.
    assert len(stalled_writer.sent) == 2
    assert len(opened) == 2
