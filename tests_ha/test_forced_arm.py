"""Tests for the inim_prime.arm_forced action (forced arming, native only)."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import (
    AreaMode,
    Local6004Config,
    Local6004Error,
    Local6004Structure,
    NativeCommandNotSent,
    NativeCommandUncertain,
    NativeObject,
    NativeZoneDef,
    NativeZoneStatus,
    SceneDef,
    ZoneState,
)
from custom_components.inim_prime.client.local6004 import (
    ZONE_OPT_AUTO_BYPASS,
    ZONE_OPT_INTERIOR,
    ZONE_OPT_UNBYPASSABLE,
)
from custom_components.inim_prime.const import DOMAIN
from custom_components.inim_prime.forced_arm import (
    EVENT_FORCED_ARM,
    SERVICE_ARM_FORCED,
    async_register_services,
)

READY = NativeZoneStatus(state=ZoneState.READY, excluded=False, alarm_memory=False)
OPEN = NativeZoneStatus(state=ZoneState.ALARM, excluded=False, alarm_memory=False)


def _zone(zone_id: int, areas: tuple[int, ...] = (1,), options: int | None = None) -> NativeZoneDef:
    return NativeZoneDef(zone_id, f"Zone {zone_id}", zone_id, areas, options=options)


@pytest.fixture
def mock_local_config() -> Local6004Config:
    """Two areas; scenario 1 arms area 1 away, scenario 2 has no single mode."""
    return Local6004Config(
        firmware="4.07 PX020",
        layout_ok=True,
        scenes=[SceneDef(id=1, arms={1: "away"}), SceneDef(id=2, arms={1: "away+stay"})],
        structure=Local6004Structure(
            areas=[NativeObject(1, "Home"), NativeObject(2, "Other")],
            zones=[
                _zone(1),
                _zone(5),
                _zone(6),
                _zone(7, options=ZONE_OPT_UNBYPASSABLE),
                _zone(8, options=ZONE_OPT_AUTO_BYPASS),  # the panel bypasses it
                _zone(9, areas=(2,)),  # not armed by scenario 1
                _zone(10, options=ZONE_OPT_INTERIOR),
            ],
            scenarios=[NativeObject(1, "Away"), NativeObject(2, "Mixed")],
            outputs=[],
        ),
    )


@pytest.fixture
def zone_statuses() -> dict[int, NativeZoneStatus]:
    """Every zone ready."""
    return dict.fromkeys((1, 5, 6, 7, 8, 9, 10), READY)


@pytest.fixture
def panel(zone_statuses: dict[int, NativeZoneStatus], mock_local_client: AsyncMock) -> AsyncMock:
    """The native mock, applying every bypass to the live zone statuses."""

    async def _bypass(zone_id: int, excluded: bool) -> None:
        zone_statuses[zone_id] = replace(zone_statuses[zone_id], excluded=excluded)

    mock_local_client.async_set_zone_bypass.side_effect = _bypass
    return mock_local_client


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _eid(hass: HomeAssistant, entry: MockConfigEntry, platform: str, suffix: str) -> str:
    eid = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{suffix}")
    assert eid is not None
    return eid


async def _call(hass: HomeAssistant, data: dict, *, response: bool = True):
    return await hass.services.async_call(
        DOMAIN, SERVICE_ARM_FORCED, data, blocking=True, return_response=response
    )


async def _target(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    await _setup(hass, entry)
    return _eid(hass, entry, "sensor", "sensor_supply_voltage")


async def test_scenario_no_open_zones_arms_cleanly(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    panel: AsyncMock,
) -> None:
    """A scenario with no open zones arms with no bypass."""
    sel = await _target(hass, mock_config_entry)

    result = await _call(hass, {"entity_id": sel, "scenario": "Away"})

    assert result == {
        "armed": True,
        "kind": "scenario",
        "bypassed_zones": [],
        "unbypassable_zones": [],
    }
    # The zones the arming checks: area 1's, less the auto-bypass zone.
    assert panel.async_get_zone_statuses.await_args_list[-2].args == ({1, 5, 6, 7, 10},)
    panel.async_set_area_modes.assert_awaited_once_with({1: AreaMode.TOTAL})
    panel.async_set_zone_bypass.assert_not_awaited()


async def test_scenario_bypasses_open_zone_then_arms(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    panel: AsyncMock,
    zone_statuses: dict[int, NativeZoneStatus],
) -> None:
    """An open, bypassable zone is excluded and verified, then the scenario armed."""
    sel = await _target(hass, mock_config_entry)
    zone_statuses[5] = OPEN
    zone_statuses[6] = replace(OPEN, excluded=True)  # open but already bypassed
    zone_statuses[8] = OPEN  # auto-bypass: the panel handles it
    zone_statuses[9] = OPEN  # another area
    events: list = []
    hass.bus.async_listen(EVENT_FORCED_ARM, lambda e: events.append(e))

    result = await _call(hass, {"entity_id": sel, "scenario": "Away"})

    panel.async_set_zone_bypass.assert_awaited_once_with(5, True)
    panel.async_set_area_modes.assert_awaited_once_with({1: AreaMode.TOTAL})
    assert result["bypassed_zones"] == [{"id": 5, "label": "Zone 5"}]
    assert result["unbypassable_zones"] == []
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["entry_id"] == mock_config_entry.entry_id
    assert events[0].data["bypassed_zones"] == [{"id": 5, "label": "Zone 5"}]


async def test_unreported_zone_counts_as_open(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    panel: AsyncMock,
    zone_statuses: dict[int, NativeZoneStatus],
) -> None:
    """A zone the live read does not report is treated as open, never as ready."""
    sel = await _target(hass, mock_config_entry)
    del zone_statuses[7]

    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, {"entity_id": sel, "scenario": "Away"})

    assert exc.value.translation_key == "forced_arm_unbypassable_zones"
    assert exc.value.translation_placeholders == {"zones": "Zone 7"}


async def test_fail_closed_unbypassable_rolls_back_and_raises(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    panel: AsyncMock,
    zone_statuses: dict[int, NativeZoneStatus],
) -> None:
    """An unbypassable open zone fails closed: nothing armed, bypass rolled back."""
    sel = await _target(hass, mock_config_entry)
    zone_statuses[5] = OPEN
    zone_statuses[7] = OPEN  # "Non escludibile": not even tried

    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, {"entity_id": sel, "scenario": "Away"})

    assert exc.value.translation_key == "forced_arm_unbypassable_zones"
    assert exc.value.translation_placeholders == {"zones": "Zone 7"}
    panel.async_set_area_modes.assert_not_awaited()
    assert [c.args for c in panel.async_set_zone_bypass.await_args_list] == [
        (5, True),
        (5, False),  # rolled back
    ]
    assert not zone_statuses[5].excluded


@pytest.mark.parametrize("failure", ["not_sent", "not_applied"])
async def test_allow_partial_arms_with_unbypassable_reported(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    panel: AsyncMock,
    zone_statuses: dict[int, NativeZoneStatus],
    failure: str,
) -> None:
    """allow_partial arms anyway and reports the zones that stayed open."""
    sel = await _target(hass, mock_config_entry)
    zone_statuses[5] = OPEN
    zone_statuses[6] = OPEN
    applies = panel.async_set_zone_bypass.side_effect

    async def _bypass(zone_id: int, excluded: bool) -> None:
        if zone_id != 5:
            await applies(zone_id, excluded)
        elif failure == "not_sent":
            raise NativeCommandNotSent(9, "refused")
        # not_applied: the panel acknowledges but the zone stays open

    panel.async_set_zone_bypass.side_effect = _bypass

    result = await _call(hass, {"entity_id": sel, "scenario": "Away", "allow_partial": True})

    panel.async_set_area_modes.assert_awaited_once_with({1: AreaMode.TOTAL})
    assert result["bypassed_zones"] == [{"id": 6, "label": "Zone 6"}]
    assert result["unbypassable_zones"] == [{"id": 5, "label": "Zone 5"}]


async def test_uncertain_bypass_is_decided_by_the_read_back(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    panel: AsyncMock,
    zone_statuses: dict[int, NativeZoneStatus],
) -> None:
    """A bypass whose answer was lost but that took effect counts as bypassed."""
    sel = await _target(hass, mock_config_entry)
    zone_statuses[5] = OPEN
    applies = panel.async_set_zone_bypass.side_effect

    async def _bypass(zone_id: int, excluded: bool) -> None:
        await applies(zone_id, excluded)
        raise NativeCommandUncertain(9, "TimeoutError")

    panel.async_set_zone_bypass.side_effect = _bypass

    result = await _call(hass, {"entity_id": sel, "scenario": "Away"})

    assert result["bypassed_zones"] == [{"id": 5, "label": "Zone 5"}]
    panel.async_set_area_modes.assert_awaited_once()


async def test_area_target_arms_with_mode(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    panel: AsyncMock,
    zone_statuses: dict[int, NativeZoneStatus],
) -> None:
    """An area target arms that area in the mapped mode; partial skips interior zones."""
    await _setup(hass, mock_config_entry)
    area = _eid(hass, mock_config_entry, "alarm_control_panel", "area_1")
    zone_statuses[10] = OPEN  # interior: ignored by night (snapshot) arming

    result = await _call(hass, {"entity_id": area, "mode": "night"})

    panel.async_set_area_modes.assert_awaited_once_with({1: AreaMode.SNAPSHOT})
    panel.async_set_zone_bypass.assert_not_awaited()
    assert result["kind"] == "area"

    await _call(hass, {"entity_id": area})  # no mode: away
    assert panel.async_set_area_modes.await_args.args == ({1: AreaMode.TOTAL},)
    panel.async_set_zone_bypass.assert_awaited_once_with(10, True)


async def test_arm_not_sent_rolls_back_and_is_command_failed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    panel: AsyncMock,
    zone_statuses: dict[int, NativeZoneStatus],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An arm that never left rolls the bypass back; a failed rollback is logged."""
    sel = await _target(hass, mock_config_entry)
    zone_statuses[5] = OPEN
    zone_statuses[6] = OPEN
    applies = panel.async_set_zone_bypass.side_effect

    async def _bypass(zone_id: int, excluded: bool) -> None:
        if zone_id == 6 and not excluded:
            raise NativeCommandNotSent(9, "refused")
        await applies(zone_id, excluded)

    panel.async_set_zone_bypass.side_effect = _bypass
    panel.async_set_area_modes.side_effect = NativeCommandNotSent(3, "refused")

    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, {"entity_id": sel, "scenario": "Away"})

    assert exc.value.translation_key == "command_failed"
    assert not zone_statuses[5].excluded  # rolled back
    assert zone_statuses[6].excluded  # its rollback failed...
    assert "Could not re-include zone 6" in caplog.text  # ...and says so


async def test_arm_uncertain_keeps_the_bypasses(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    panel: AsyncMock,
    zone_statuses: dict[int, NativeZoneStatus],
) -> None:
    """The panel may be armed: re-including an open zone could trip it."""
    sel = await _target(hass, mock_config_entry)
    zone_statuses[5] = OPEN
    panel.async_set_area_modes.side_effect = NativeCommandUncertain(3, "TimeoutError")
    reads = panel.async_get_area_statuses.await_count

    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, {"entity_id": sel, "scenario": "Away"})

    assert exc.value.translation_key == "native_command_uncertain"
    panel.async_set_zone_bypass.assert_awaited_once_with(5, True)
    assert zone_statuses[5].excluded
    assert panel.async_get_area_statuses.await_count == reads + 1  # re-read


async def test_zone_read_failure_is_command_failed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    panel: AsyncMock,
) -> None:
    sel = await _target(hass, mock_config_entry)
    panel.async_get_zone_statuses.side_effect = Local6004Error("down")

    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, {"entity_id": sel, "scenario": "Away"})

    assert exc.value.translation_key == "command_failed"
    panel.async_set_area_modes.assert_not_awaited()


async def test_unverified_firmware_is_refused(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    panel: AsyncMock,
) -> None:
    sel = await _target(hass, mock_config_entry)
    panel.supports_native_writes = False

    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, {"entity_id": sel, "scenario": "Away"})

    assert exc.value.translation_key == "command_failed"
    panel.async_set_area_modes.assert_not_awaited()


async def test_scenario_without_a_single_mode_is_refused(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    panel: AsyncMock,
) -> None:
    sel = await _target(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, {"entity_id": sel, "scenario": "Mixed"})

    assert exc.value.translation_key == "scenario_not_supported"
    panel.async_set_area_modes.assert_not_awaited()


async def test_validation_errors(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    panel: AsyncMock,
) -> None:
    """All the ServiceValidationError branches."""
    sel = await _target(hass, mock_config_entry)
    area = _eid(hass, mock_config_entry, "alarm_control_panel", "area_1")

    # two targets -> single_target
    with pytest.raises(ServiceValidationError) as e1:
        await _call(hass, {"entity_id": [sel, area], "scenario": "Away"}, response=False)
    assert e1.value.translation_key == "forced_arm_single_target"

    # scenario + mode -> mode_with_scenario
    with pytest.raises(ServiceValidationError) as e2:
        await _call(hass, {"entity_id": sel, "scenario": "Away", "mode": "away"}, response=False)
    assert e2.value.translation_key == "forced_arm_mode_with_scenario"

    # unknown scenario
    with pytest.raises(ServiceValidationError) as e3:
        await _call(hass, {"entity_id": sel, "scenario": "Nope"}, response=False)
    assert e3.value.translation_key == "forced_arm_unknown_scenario"

    # non-area inim entity without scenario -> area_or_scenario
    with pytest.raises(ServiceValidationError) as e4:
        await _call(hass, {"entity_id": sel, "mode": "away"}, response=False)
    assert e4.value.translation_key == "forced_arm_area_or_scenario"


async def test_bad_target_not_inim_entity(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    panel: AsyncMock,
) -> None:
    """A non-existent and a foreign-domain entity both raise forced_arm_bad_target."""
    await _setup(hass, mock_config_entry)

    # non-existent entity -> registry returns None
    with pytest.raises(ServiceValidationError) as e1:
        await _call(
            hass, {"entity_id": "select.does_not_exist", "scenario": "Away"}, response=False
        )
    assert e1.value.translation_key == "forced_arm_bad_target"

    # entity owned by a different (non-inim) config entry
    other = MockConfigEntry(domain="other")
    other.add_to_hass(hass)
    reg = er.async_get(hass)
    foreign = reg.async_get_or_create("sensor", "other", "x", config_entry=other).entity_id
    with pytest.raises(ServiceValidationError) as e2:
        await _call(hass, {"entity_id": foreign, "scenario": "Away"}, response=False)
    assert e2.value.translation_key == "forced_arm_bad_target"


async def test_register_services_idempotent(hass: HomeAssistant) -> None:
    """Registering twice does not raise and leaves one service."""
    async_register_services(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_ARM_FORCED)
    async_register_services(hass)  # second call short-circuits
    assert hass.services.has_service(DOMAIN, SERVICE_ARM_FORCED)
