"""Tests for the INIM Prime switch platform."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache

from custom_components.inim_prime.client import (
    Fault,
    Local6004Structure,
    NativeCommandNotSent,
    NativeCommandUncertain,
    NativeZoneStatus,
    Output,
    PanelStatus,
    Power,
    Version,
    Zone,
    ZoneState,
)
from custom_components.inim_prime.const import DOMAIN
from custom_components.inim_prime.coordinator import InimData
from custom_components.inim_prime.switch import (
    InimOutputSwitch,
    InimZoneBypassSwitch,
    async_setup_entry,
)


@pytest.fixture
def outputs() -> list[Output]:
    return [
        Output(id=1, label="Siren", state=0),
        Output(id=2, label="Gate", state=3),
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
def fake_coordinator(outputs, zones):
    """Build a fake coordinator with sample InimData and a native client."""
    data = InimData(
        version=Version(raw="4.07 PX500"),
        areas=[],
        zones=zones,
        scenarios=[],
        outputs=outputs,
        fault=Fault(vcc=13.5, has_faults=False),
        power=Power(battery_voltage=13.4),
        panel=PanelStatus(service=False, programming=False),
    )
    client = AsyncMock()
    client.supports_native_writes = True
    # The bypass read-back finds no status: it says nothing either way.
    client.async_get_zone_statuses.return_value = {}
    return SimpleNamespace(
        data=data,
        client=client,
        config_entry=SimpleNamespace(entry_id="abc123", title="INIM Prime", options={}),
        hass=SimpleNamespace(config=SimpleNamespace(language="en")),
        structure=Local6004Structure(),
        force_arm_on_open=False,
        last_update_success=True,
        async_refresh=AsyncMock(),
        async_add_listener=lambda *a, **k: lambda: None,
    )


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
    assert off.available is True
    di = off.device_info
    assert di["identifiers"] == {("inim_prime", "abc123")}
    assert di["manufacturer"] == "INIM"
    assert di["model"] == "PX500"
    assert di["sw_version"] == "4.07"
    assert di["name"] == "INIM Prime"


def test_output_switch_unknown_state(fake_coordinator, entry):
    """An output whose state has not been read yet has no known on/off state."""
    fake_coordinator.data.outputs.append(Output(id=1507, label="Output 3", state=None))
    switch = InimOutputSwitch(fake_coordinator, entry, 1507)
    assert switch.is_on is None
    assert switch.name == "Output 3"


def test_switches_of_missing_objects(fake_coordinator, entry):
    """A switch whose output or zone is gone is unavailable and reports nothing."""
    output = InimOutputSwitch(fake_coordinator, entry, 99)
    bypass = InimZoneBypassSwitch(fake_coordinator, entry, 99)
    for switch in (output, bypass):
        assert switch.available is False
        assert switch.name is None
        assert switch.is_on is None


def test_zone_bypass_switch(fake_coordinator, entry):
    not_bypassed = InimZoneBypassSwitch(fake_coordinator, entry, 1)
    bypassed = InimZoneBypassSwitch(fake_coordinator, entry, 2)

    assert not_bypassed.is_on is False
    assert bypassed.is_on is True
    assert not_bypassed.available is True
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
    """Turning an output on/off sends op 8 natively, then re-reads the panel."""
    sw = InimOutputSwitch(fake_coordinator, entry, 1)

    await sw.async_turn_on()
    fake_coordinator.client.async_set_output.assert_awaited_with(1, True)

    await sw.async_turn_off()
    fake_coordinator.client.async_set_output.assert_awaited_with(1, False)

    assert fake_coordinator.async_refresh.await_count == 2


async def test_output_not_sent_raises_command_failed(fake_coordinator, entry):
    """A command that never left raises command_failed, without a re-read."""
    fake_coordinator.client.async_set_output.side_effect = NativeCommandNotSent(8, "refused")
    sw = InimOutputSwitch(fake_coordinator, entry, 1)

    with pytest.raises(HomeAssistantError) as exc_info:
        await sw.async_turn_on()
    assert exc_info.value.translation_key == "command_failed"
    fake_coordinator.async_refresh.assert_not_awaited()


async def test_output_uncertain_raises_and_rereads(fake_coordinator, entry):
    """A command that may have reached the panel is not retried: re-read and raise."""
    fake_coordinator.client.async_set_output.side_effect = NativeCommandUncertain(8, "timeout")
    sw = InimOutputSwitch(fake_coordinator, entry, 1)

    with pytest.raises(HomeAssistantError) as exc_info:
        await sw.async_turn_on()
    assert exc_info.value.translation_key == "native_command_uncertain"
    fake_coordinator.client.async_set_output.assert_awaited_once()
    fake_coordinator.async_refresh.assert_awaited_once()


async def test_zone_bypass_turn_on_off(fake_coordinator, entry):
    """Bypass on/off sends op 9 natively, then re-reads the panel."""
    sw = InimZoneBypassSwitch(fake_coordinator, entry, 1)

    await sw.async_turn_on()
    fake_coordinator.client.async_set_zone_bypass.assert_awaited_with(1, True)

    await sw.async_turn_off()
    fake_coordinator.client.async_set_zone_bypass.assert_awaited_with(1, False)

    assert fake_coordinator.async_refresh.await_count == 2


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


async def test_bypass_switch_end_to_end(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    zone_statuses: dict[int, NativeZoneStatus],
) -> None:
    """Turning the bypass switch on bypasses the zone and the switch reads on."""

    async def _bypass(zone_id: int, excluded: bool) -> None:
        zone_statuses[zone_id] = NativeZoneStatus(
            state=ZoneState.READY, excluded=excluded, alarm_memory=False
        )

    patch_client.async_set_zone_bypass.side_effect = _bypass
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{mock_config_entry.entry_id}_zone_bypass_1"
    )
    assert hass.states.get(entity_id).state == "off"

    await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done()

    patch_client.async_set_zone_bypass.assert_awaited_once_with(1, True)
    assert hass.states.get(entity_id).state == "on"


# --------------------------------------------------------------- force-arm switch
async def _find_force_switch(hass, entry):
    reg = er.async_get(hass)
    for e in er.async_entries_for_config_entry(reg, entry.entry_id):
        if e.unique_id.endswith("_force_arm_on_open"):
            return e.entity_id
    return None


async def test_force_arm_switch_toggle(hass, mock_config_entry, patch_client) -> None:
    """The preference switch defaults off and mirrors onto the coordinator."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_config_entry.runtime_data.coordinator

    eid = await _find_force_switch(hass, mock_config_entry)
    assert eid is not None
    assert hass.states.get(eid).state == "off"
    assert coordinator.force_arm_on_open is False

    await hass.services.async_call("switch", "turn_on", {"entity_id": eid}, blocking=True)
    assert hass.states.get(eid).state == "on"
    assert coordinator.force_arm_on_open is True

    await hass.services.async_call("switch", "turn_off", {"entity_id": eid}, blocking=True)
    assert hass.states.get(eid).state == "off"
    assert coordinator.force_arm_on_open is False


async def test_force_arm_switch_restores_on(hass, mock_config_entry, patch_client) -> None:
    """A restored 'on' state is applied and mirrored onto the coordinator."""
    mock_restore_cache(hass, [State("switch.inim_prime_force_arm_on_open_zones", "on")])
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    eid = await _find_force_switch(hass, mock_config_entry)
    assert hass.states.get(eid).state == "on"
    assert mock_config_entry.runtime_data.coordinator.force_arm_on_open is True
