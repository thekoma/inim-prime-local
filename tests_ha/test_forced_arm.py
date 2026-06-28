"""Tests for the inim_prime.arm_forced action (forced arming)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from custom_components.inim_prime.client import (
    ApiStatus,
    InimApiError,
    InimConnectionError,
    OpenZone,
    Zone,
    ZoneState,
)

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.const import DOMAIN
from custom_components.inim_prime.forced_arm import (
    EVENT_FORCED_ARM,
    SERVICE_ARM_FORCED,
    async_register_services,
)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _eid(hass: HomeAssistant, entry: MockConfigEntry, platform: str, suffix: str) -> str:
    eid = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{entry.entry_id}_{suffix}"
    )
    assert eid is not None
    return eid


def _zone(zone_id: int, excluded: bool) -> Zone:
    return Zone(
        id=zone_id,
        label=f"Zone {zone_id}",
        terminal=zone_id,
        state=ZoneState.READY,
        alarm_memory=False,
        excluded=excluded,
    )


async def _call(hass: HomeAssistant, data: dict, *, response: bool = True):
    return await hass.services.async_call(
        DOMAIN, SERVICE_ARM_FORCED, data, blocking=True, return_response=response
    )


async def test_scenario_no_open_zones_arms_cleanly(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """A scenario with no open zones arms with no bypass."""
    patch_client.get_scenario_open_zones.return_value = []
    entry = await _setup(hass, mock_config_entry, patch_client)
    sel = _eid(hass, entry, "select", "select_scenario")

    result = await _call(hass, {"entity_id": sel, "scenario": "Away"})

    assert result == {"armed": True, "kind": "scenario", "bypassed_zones": [], "unbypassable_zones": []}
    patch_client.apply_scenario.assert_awaited_once_with(1)
    patch_client.set_zone_excluded.assert_not_awaited()


async def test_scenario_bypasses_open_zone_then_arms(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """An open, bypassable zone is excluded, then the scenario is applied."""
    patch_client.get_scenario_open_zones.return_value = [OpenZone(id=5, label="Window")]
    # Re-read shows the zone is now excluded -> bypass took.
    patch_client.get_zones.return_value = [_zone(5, excluded=True)]
    entry = await _setup(hass, mock_config_entry, patch_client)
    sel = _eid(hass, entry, "select", "select_scenario")

    events: list = []
    hass.bus.async_listen(EVENT_FORCED_ARM, lambda e: events.append(e))

    result = await _call(hass, {"entity_id": sel, "scenario": "Away"})

    patch_client.set_zone_excluded.assert_awaited_once_with(5, True)
    patch_client.apply_scenario.assert_awaited_once_with(1)
    assert result["bypassed_zones"] == [{"id": 5, "label": "Window"}]
    assert result["unbypassable_zones"] == []
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["entry_id"] == entry.entry_id


async def test_fail_closed_unbypassable_rolls_back_and_raises(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """An unbypassable open zone fails closed: nothing armed, bypass rolled back."""
    patch_client.get_scenario_open_zones.return_value = [
        OpenZone(id=5, label="Window"),
        OpenZone(id=6, label="Skylight"),
    ]
    # Zone 5 bypass rejected; zone 6 succeeds (excluded on re-read).
    patch_client.set_zone_excluded.side_effect = [
        InimApiError(ApiStatus.CODE_NOT_ALLOWED),
        None,
        None,  # rollback of zone 6
    ]
    patch_client.get_zones.return_value = [_zone(5, excluded=False), _zone(6, excluded=True)]
    entry = await _setup(hass, mock_config_entry, patch_client)
    sel = _eid(hass, entry, "select", "select_scenario")

    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, {"entity_id": sel, "scenario": "Away"})
    assert exc.value.translation_key == "forced_arm_unbypassable_zones"
    patch_client.apply_scenario.assert_not_awaited()
    # The successfully-bypassed zone 6 is rolled back (un-excluded).
    patch_client.set_zone_excluded.assert_any_await(6, False)


async def test_allow_partial_arms_with_unbypassable_reported(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """allow_partial arms anyway and reports the zones that stayed open."""
    patch_client.get_scenario_open_zones.return_value = [OpenZone(id=5, label="Window")]
    # Write 'succeeds' but re-read shows still not excluded -> unbypassable.
    patch_client.get_zones.return_value = [_zone(5, excluded=False)]
    entry = await _setup(hass, mock_config_entry, patch_client)
    sel = _eid(hass, entry, "select", "select_scenario")

    result = await _call(
        hass, {"entity_id": sel, "scenario": "Away", "allow_partial": True}
    )

    patch_client.apply_scenario.assert_awaited_once_with(1)
    assert result["bypassed_zones"] == []
    assert result["unbypassable_zones"] == [{"id": 5, "label": "Window"}]


async def test_area_target_arms_with_mode(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """An area target force-arms via arm_area with the mapped ArmMode."""
    from custom_components.inim_prime.client import ArmMode

    patch_client.get_area_open_zones.return_value = []
    entry = await _setup(hass, mock_config_entry, patch_client)
    panel = _eid(hass, entry, "alarm_control_panel", "area_1")

    result = await _call(hass, {"entity_id": panel, "mode": "night"})

    patch_client.arm_area.assert_awaited_once_with(1, ArmMode.SNAPSHOT)
    assert result["kind"] == "area"


async def test_arm_failure_rolls_back_and_translates(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """A panel rejection during arm rolls back the bypass and translates the error."""
    patch_client.get_scenario_open_zones.return_value = [OpenZone(id=5, label="Window")]
    patch_client.get_zones.return_value = [_zone(5, excluded=True)]
    patch_client.apply_scenario.side_effect = InimApiError(ApiStatus.ZONES_NOT_READY)
    # Bypass succeeds; the rollback write then fails and must be swallowed.
    patch_client.set_zone_excluded.side_effect = [None, InimConnectionError("rollback")]
    entry = await _setup(hass, mock_config_entry, patch_client)
    sel = _eid(hass, entry, "select", "select_scenario")

    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, {"entity_id": sel, "scenario": "Away"})
    assert exc.value.translation_key == "zones_not_ready"
    patch_client.set_zone_excluded.assert_any_await(5, False)  # rollback attempted


async def test_arm_failure_connection_error_is_command_failed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """A connection error during arm maps to command_failed."""
    patch_client.get_scenario_open_zones.return_value = []
    patch_client.apply_scenario.side_effect = InimConnectionError("down")
    entry = await _setup(hass, mock_config_entry, patch_client)
    sel = _eid(hass, entry, "select", "select_scenario")

    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, {"entity_id": sel, "scenario": "Away"})
    assert exc.value.translation_key == "command_failed"


async def test_validation_errors(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """All the ServiceValidationError branches."""
    patch_client.get_scenario_open_zones.return_value = []
    entry = await _setup(hass, mock_config_entry, patch_client)
    sel = _eid(hass, entry, "select", "select_scenario")
    panel = _eid(hass, entry, "alarm_control_panel", "area_1")

    # two targets -> single_target
    with pytest.raises(ServiceValidationError) as e1:
        await _call(hass, {"entity_id": [sel, panel], "scenario": "Away"}, response=False)
    assert e1.value.translation_key == "forced_arm_single_target"

    # scenario + mode -> mode_with_scenario
    with pytest.raises(ServiceValidationError) as e2:
        await _call(hass, {"entity_id": sel, "scenario": "Away", "mode": "away"}, response=False)
    assert e2.value.translation_key == "forced_arm_mode_with_scenario"

    # unknown scenario
    with pytest.raises(ServiceValidationError) as e3:
        await _call(hass, {"entity_id": sel, "scenario": "Nope"}, response=False)
    assert e3.value.translation_key == "forced_arm_unknown_scenario"

    # select target without scenario and without being an area -> area_or_scenario
    with pytest.raises(ServiceValidationError) as e4:
        await _call(hass, {"entity_id": sel, "mode": "away"}, response=False)
    assert e4.value.translation_key == "forced_arm_area_or_scenario"


async def test_bad_target_not_inim_entity(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """A non-existent and a foreign-domain entity both raise forced_arm_bad_target."""
    await _setup(hass, mock_config_entry, patch_client)

    # non-existent entity -> registry returns None
    with pytest.raises(ServiceValidationError) as e1:
        await _call(hass, {"entity_id": "select.does_not_exist", "scenario": "Away"}, response=False)
    assert e1.value.translation_key == "forced_arm_bad_target"

    # entity owned by a different (non-inim) config entry
    other = MockConfigEntry(domain="other")
    other.add_to_hass(hass)
    reg = er.async_get(hass)
    foreign = reg.async_get_or_create(
        "sensor", "other", "x", config_entry=other
    ).entity_id
    with pytest.raises(ServiceValidationError) as e2:
        await _call(hass, {"entity_id": foreign, "scenario": "Away"}, response=False)
    assert e2.value.translation_key == "forced_arm_bad_target"


async def test_register_services_idempotent(hass: HomeAssistant) -> None:
    """Registering twice does not raise and leaves one service."""
    async_register_services(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_ARM_FORCED)
    async_register_services(hass)  # second call short-circuits
    assert hass.services.has_service(DOMAIN, SERVICE_ARM_FORCED)
