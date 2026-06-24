"""Tests for the INIM Prime local realtime webhook + adaptive polling."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from custom_components.inim_prime.client import (
    AreaMode,
    AreaState,
    Zone,
    ZoneState,
)

from homeassistant.components import webhook
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.const import (
    CONF_APIKEY,
    CONF_SCAN_INTERVAL_ACTIVE,
    CONF_SCAN_INTERVAL_IDLE,
    CONF_USE_HTTPS,
    CONF_WEBHOOK_ENABLED,
    CONF_WEBHOOK_ID,
    DOMAIN,
)
from custom_components.inim_prime.coordinator import (
    InimData,
    InimDataUpdateCoordinator,
)

WEBHOOK_ID = "inim_test_secret_abcdef"


@pytest.fixture
def push_entry() -> MockConfigEntry:
    """A config entry with push mode enabled and a webhook id."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="INIM Prime",
        data={
            "host": "192.0.2.10",
            "port": 8080,
            CONF_APIKEY: "secret-key",
            CONF_USE_HTTPS: False,
            "scan_interval": 15,
        },
        options={
            CONF_WEBHOOK_ENABLED: True,
            CONF_WEBHOOK_ID: WEBHOOK_ID,
            CONF_SCAN_INTERVAL_IDLE: 30,
            CONF_SCAN_INTERVAL_ACTIVE: 3,
        },
        unique_id="192.0.2.10:8080",
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, mock_client: AsyncMock):
    """Set up the integration and return the coordinator."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data.coordinator


def _panel_reports_zone1_open(mock_client: AsyncMock) -> None:
    """Make the reconciliation poll agree that zone 1 is now open.

    The webhook applies an optimistic patch immediately; the follow-up
    reconcile poll re-reads the panel, so for an end-to-end assertion the
    mocked panel must report the same post-event state (as a real panel
    would).
    """
    mock_client.get_zones.return_value = [
        Zone(
            id=1,
            label="Front Door",
            terminal=1,
            state=ZoneState.ALARM,
            alarm_memory=False,
            excluded=False,
        )
    ]


# ---------------------------------------------------------------------------
# apply_event (coordinator patch helper)
# ---------------------------------------------------------------------------
def _make_coordinator(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> InimDataUpdateCoordinator:
    entry.add_to_hass(hass)
    return InimDataUpdateCoordinator(hass, entry, client)


def _seed(coordinator: InimDataUpdateCoordinator, data: InimData) -> None:
    coordinator.data = data


async def test_apply_event_zone_open_close(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """zone_open/zone_close flip the targeted zone's state immutably."""
    coordinator = _make_coordinator(hass, mock_config_entry, mock_client)
    original = await coordinator._async_update_data()
    _seed(coordinator, original)

    opened = coordinator.apply_event("zone_open", id="1")
    assert opened is not None
    assert opened.zones[0].state is ZoneState.ALARM
    # Original snapshot is untouched (immutability).
    assert original.zones[0].state is ZoneState.READY

    _seed(coordinator, opened)
    closed = coordinator.apply_event("zone_close", id="1")
    assert closed.zones[0].state is ZoneState.READY


async def test_apply_event_arm_disarm_alarm(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """arm/disarm change area mode; alarm sets memory + ALARM state."""
    coordinator = _make_coordinator(hass, mock_config_entry, mock_client)
    _seed(coordinator, await coordinator._async_update_data())

    armed = coordinator.apply_event("arm", area="1")
    assert armed.areas[0].mode is AreaMode.TOTAL

    _seed(coordinator, armed)
    disarmed = coordinator.apply_event("disarm", area="1")
    assert disarmed.areas[0].mode is AreaMode.DISARMED

    _seed(coordinator, disarmed)
    alarmed = coordinator.apply_event("alarm", area="1")
    assert alarmed.areas[0].alarm_memory is True
    assert alarmed.areas[0].state is AreaState.ALARM

    # A disarm after an alarm must clear the latched alarm state/memory,
    # otherwise the panel would still render TRIGGERED (state in ALARM).
    _seed(coordinator, alarmed)
    disarmed_after_alarm = coordinator.apply_event("disarm", area="1")
    assert disarmed_after_alarm.areas[0].mode is AreaMode.DISARMED
    assert disarmed_after_alarm.areas[0].state is AreaState.READY
    assert disarmed_after_alarm.areas[0].alarm_memory is False

    # arm likewise resets state to a non-alarm value.
    _seed(coordinator, alarmed)
    armed_after_alarm = coordinator.apply_event("arm", area="1")
    assert armed_after_alarm.areas[0].state is AreaState.READY


async def test_apply_event_output_and_fault(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """output sets the relay state; fault/restore toggle has_faults."""
    coordinator = _make_coordinator(hass, mock_config_entry, mock_client)
    _seed(coordinator, await coordinator._async_update_data())

    out = coordinator.apply_event("output", id="1", state="1")
    assert out.outputs[0].state == 1

    faulted = coordinator.apply_event("fault", code="mains")
    assert faulted.fault.has_faults is True

    _seed(coordinator, faulted)
    restored = coordinator.apply_event("fault_restore", code="mains")
    assert restored.fault.has_faults is False


async def test_apply_event_unknown_or_missing_returns_none(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """Unknown targets / bad ids return None (poll reconciles)."""
    coordinator = _make_coordinator(hass, mock_config_entry, mock_client)
    _seed(coordinator, await coordinator._async_update_data())

    assert coordinator.apply_event("zone_open", id="999") is None
    assert coordinator.apply_event("zone_open", id="not-a-number") is None
    assert coordinator.apply_event("arm") is None
    assert coordinator.apply_event("bogus_event", id="1") is None


async def test_apply_event_no_data_returns_none(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """With no cached snapshot yet, apply_event returns None."""
    coordinator = _make_coordinator(hass, mock_config_entry, mock_client)
    assert coordinator.data is None
    assert coordinator.apply_event("zone_open", id="1") is None


# ---------------------------------------------------------------------------
# Adaptive interval selection
# ---------------------------------------------------------------------------
async def test_idle_interval_from_options(
    hass: HomeAssistant,
    push_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """The idle interval is taken from the idle option."""
    coordinator = _make_coordinator(hass, push_entry, mock_client)
    assert coordinator.update_interval == timedelta(seconds=30)


async def test_legacy_scan_interval_still_used_as_idle(
    hass: HomeAssistant,
    mock_client: AsyncMock,
) -> None:
    """Without the new options, the legacy scan_interval option drives idle."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="INIM Prime",
        data={
            "host": "192.0.2.10",
            "port": 8080,
            CONF_APIKEY: "secret-key",
            CONF_USE_HTTPS: False,
            "scan_interval": 15,
        },
        options={"scan_interval": 42},
        unique_id="192.0.2.10:8080",
    )
    coordinator = _make_coordinator(hass, entry, mock_client)
    assert coordinator.update_interval == timedelta(seconds=42)


async def test_activate_fast_poll_then_decays(
    hass: HomeAssistant,
    push_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """activate_fast_poll switches to the active interval, then decays."""
    coordinator = _make_coordinator(hass, push_entry, mock_client)
    assert coordinator.update_interval == timedelta(seconds=30)

    coordinator.activate_fast_poll()
    assert coordinator.update_interval == timedelta(seconds=3)

    coordinator._decay_to_idle()
    assert coordinator.update_interval == timedelta(seconds=30)


# ---------------------------------------------------------------------------
# End-to-end webhook via the HA test client
# ---------------------------------------------------------------------------
async def test_webhook_zone_open_updates_entity(
    hass: HomeAssistant,
    push_entry: MockConfigEntry,
    mock_client: AsyncMock,
    patch_client: AsyncMock,
    hass_client_no_auth,
) -> None:
    """A panel zone_open POST flips the zone binary sensor immediately."""
    coordinator = await _setup(hass, push_entry, mock_client)
    entity_id = "binary_sensor.inim_prime_front_door"
    assert hass.states.get(entity_id).state == "off"

    _panel_reports_zone1_open(mock_client)
    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}?ev=zone_open&id=1")
    assert resp.status == 200
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "on"
    # An event also kicked the fast poll tier.
    assert coordinator.update_interval == timedelta(seconds=3)


async def test_webhook_optimistic_value_survives_stale_poll(
    hass: HomeAssistant,
    push_entry: MockConfigEntry,
    mock_client: AsyncMock,
    patch_client: AsyncMock,
    hass_client_no_auth,
) -> None:
    """The optimistic patch must not be reverted by a lagging reconcile poll.

    On a real panel the poll lags the event, so the reconcile read still
    returns the OLD (pre-event) state for a while. The webhook must not trigger
    an immediate refresh that would re-read that stale state and flap the
    entity back off. Here the mocked panel keeps reporting zone 1 closed; the
    entity must still be 'on' from the optimistic patch.
    """
    coordinator = await _setup(hass, push_entry, mock_client)
    entity_id = "binary_sensor.inim_prime_front_door"
    assert hass.states.get(entity_id).state == "off"

    # Panel still reports the OLD state (zone closed) — the realistic race.
    poll_calls_before = mock_client.get_zones.call_count

    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}?ev=zone_open&id=1")
    assert resp.status == 200
    await hass.async_block_till_done()

    # Optimistic value holds even though the panel still reports closed.
    assert hass.states.get(entity_id).state == "on"
    # No immediate reconcile poll was kicked (would re-read stale state).
    assert mock_client.get_zones.call_count == poll_calls_before
    # The fast poll tier is armed to reconcile at the active cadence instead.
    assert coordinator.update_interval == timedelta(seconds=3)


async def test_webhook_get_method_accepted(
    hass: HomeAssistant,
    push_entry: MockConfigEntry,
    mock_client: AsyncMock,
    patch_client: AsyncMock,
    hass_client_no_auth,
) -> None:
    """GET works identically to POST on this firmware path."""
    await _setup(hass, push_entry, mock_client)
    _panel_reports_zone1_open(mock_client)
    client = await hass_client_no_auth()
    resp = await client.get(f"/api/webhook/{WEBHOOK_ID}?ev=zone_open&id=1")
    assert resp.status == 200
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.inim_prime_front_door").state == "on"


async def test_webhook_body_form_encoded(
    hass: HomeAssistant,
    push_entry: MockConfigEntry,
    mock_client: AsyncMock,
    patch_client: AsyncMock,
    hass_client_no_auth,
) -> None:
    """Params can arrive in the form body, not just the query string."""
    await _setup(hass, push_entry, mock_client)
    _panel_reports_zone1_open(mock_client)
    client = await hass_client_no_auth()
    resp = await client.post(
        f"/api/webhook/{WEBHOOK_ID}",
        data={"ev": "zone_open", "id": "1"},
    )
    assert resp.status == 200
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.inim_prime_front_door").state == "on"


async def test_webhook_unknown_event_ignored_200(
    hass: HomeAssistant,
    push_entry: MockConfigEntry,
    mock_client: AsyncMock,
    patch_client: AsyncMock,
    hass_client_no_auth,
) -> None:
    """An unknown event is ignored but still returns 200, no state change."""
    await _setup(hass, push_entry, mock_client)
    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}?ev=teleport&id=1")
    assert resp.status == 200
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.inim_prime_front_door").state == "off"


async def test_webhook_malformed_does_not_crash(
    hass: HomeAssistant,
    push_entry: MockConfigEntry,
    mock_client: AsyncMock,
    patch_client: AsyncMock,
    hass_client_no_auth,
) -> None:
    """A malformed call (no params, junk body) is ignored and returns 200."""
    await _setup(hass, push_entry, mock_client)
    client = await hass_client_no_auth()

    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}")
    assert resp.status == 200

    resp = await client.post(
        f"/api/webhook/{WEBHOOK_ID}",
        data=b"\x00\x01\x02 not form data",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status == 200
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.inim_prime_front_door").state == "off"


async def test_webhook_wrong_secret_ignored(
    hass: HomeAssistant,
    push_entry: MockConfigEntry,
    mock_client: AsyncMock,
    patch_client: AsyncMock,
    hass_client_no_auth,
) -> None:
    """A call to an unregistered id returns 200 and changes nothing."""
    await _setup(hass, push_entry, mock_client)
    client = await hass_client_no_auth()
    resp = await client.post("/api/webhook/inim_wrong_secret?ev=zone_open&id=1")
    assert resp.status == 200
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.inim_prime_front_door").state == "off"


async def test_webhook_unregistered_after_unload(
    hass: HomeAssistant,
    push_entry: MockConfigEntry,
    mock_client: AsyncMock,
    patch_client: AsyncMock,
    hass_client_no_auth,
) -> None:
    """Unloading the entry unregisters the webhook (id becomes reusable)."""
    await _setup(hass, push_entry, mock_client)

    assert await hass.config_entries.async_unload(push_entry.entry_id)
    await hass.async_block_till_done()

    # After unload the id is free again: re-registering must not raise.
    webhook.async_register(
        hass, "test", "probe", WEBHOOK_ID, lambda *a: None
    )
    webhook.async_unregister(hass, WEBHOOK_ID)


# ---------------------------------------------------------------------------
# Options flow: enabling push mode generates a webhook id + URL notification
# ---------------------------------------------------------------------------
async def test_options_enable_push_generates_webhook(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    patch_client: AsyncMock,
) -> None:
    """Turning on push mode in options stores a webhook id and notifies."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(
        mock_config_entry.entry_id
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL_IDLE: 30,
            CONF_SCAN_INTERVAL_ACTIVE: 3,
            CONF_WEBHOOK_ENABLED: True,
        },
    )
    await hass.async_block_till_done()

    assert mock_config_entry.options[CONF_WEBHOOK_ENABLED] is True
    webhook_id = mock_config_entry.options[CONF_WEBHOOK_ID]
    assert webhook_id

    # The webhook was registered on reload (OptionsFlowWithReload); the id is
    # taken, so a fresh registration of the same id would raise.
    with pytest.raises(ValueError):
        webhook.async_register(
            hass, "probe", "probe", webhook_id, lambda *a: None
        )
