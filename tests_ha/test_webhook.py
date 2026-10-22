"""Tests for the INIM Prime local realtime webhook and the optimistic patch."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime import webhook as webhook_mod
from custom_components.inim_prime.client import (
    AreaMode,
    AreaState,
    Local6004Config,
    NativeZoneStatus,
    ZoneState,
)
from custom_components.inim_prime.const import (
    CONF_LOCAL_PASSWORD,
    CONF_WEBHOOK_ENABLED,
    CONF_WEBHOOK_ID,
    DOMAIN,
    MAX_WEBHOOK_BODY,
)
from custom_components.inim_prime.coordinator import InimDataUpdateCoordinator

WEBHOOK_ID = "inim_test_secret_abcdef"
ZONE_ENTITY = "binary_sensor.inim_prime_front_door"


@pytest.fixture
def push_entry() -> MockConfigEntry:
    """A config entry with push mode enabled and a webhook id."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="INIM Prime",
        data={"host": "192.0.2.10", CONF_LOCAL_PASSWORD: "pass"},
        options={CONF_WEBHOOK_ENABLED: True, CONF_WEBHOOK_ID: WEBHOOK_ID},
        unique_id="192.0.2.10",
        version=2,
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> InimDataUpdateCoordinator:
    """Set up the integration and return the coordinator."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data.coordinator


# ---------------------------------------------------------------------------
# apply_event (the coordinator's optimistic patch)
# ---------------------------------------------------------------------------
async def _seeded(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: AsyncMock,
    config: Local6004Config,
) -> InimDataUpdateCoordinator:
    entry.add_to_hass(hass)
    coordinator = InimDataUpdateCoordinator(hass, entry, client, config)
    coordinator.data = await coordinator._async_update_data()
    return coordinator


async def test_apply_event_zone_open_close(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """zone_open/zone_close flip the targeted zone's state immutably."""
    coordinator = await _seeded(hass, mock_config_entry, mock_local_client, mock_local_config)
    original = coordinator.data

    opened = coordinator.apply_event("zone_open", id="1")
    assert opened is not None
    assert opened.zones[0].state is ZoneState.ALARM
    assert original.zones[0].state is ZoneState.READY  # the snapshot is untouched

    coordinator.data = opened
    closed = coordinator.apply_event("zone_close", id="1")
    assert closed is not None
    assert closed.zones[0].state is ZoneState.READY


async def test_apply_event_arm_disarm_alarm(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """arm/disarm change the area mode and clear an alarm; alarm latches it."""
    coordinator = await _seeded(hass, mock_config_entry, mock_local_client, mock_local_config)

    armed = coordinator.apply_event("arm", area="1")
    assert armed.areas[0].mode is AreaMode.TOTAL

    alarmed = coordinator.apply_event("alarm", area="1")
    assert alarmed.areas[0].alarm_memory is True
    assert alarmed.areas[0].state is AreaState.ALARM

    # A disarm after an alarm clears the latched state, or the panel entity
    # would keep rendering TRIGGERED until the next poll.
    coordinator.data = alarmed
    disarmed = coordinator.apply_event("disarm", area="1")
    assert disarmed.areas[0].mode is AreaMode.DISARMED
    assert disarmed.areas[0].state is AreaState.READY
    assert disarmed.areas[0].alarm_memory is False


async def test_apply_event_output_and_fault(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """output sets the relay state; fault/tamper/restore toggle has_faults."""
    coordinator = await _seeded(hass, mock_config_entry, mock_local_client, mock_local_config)

    assert coordinator.apply_event("output", id="1", state="1").outputs[0].state == 1
    # A non-integer state counts as on rather than failing.
    assert coordinator.apply_event("output", id="1", state="junk").outputs[0].state == 1

    assert coordinator.apply_event("fault", code="mains").fault.has_faults is True
    tampered = coordinator.apply_event("tamper")
    assert tampered.fault.has_faults is True
    coordinator.data = tampered
    assert coordinator.apply_event("fault_restore").fault.has_faults is False


async def test_apply_event_unknown_targets_return_none(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """Unknown objects, bad ids and unknown events leave reconciliation to the poll."""
    coordinator = await _seeded(hass, mock_config_entry, mock_local_client, mock_local_config)

    assert coordinator.apply_event("zone_open", id="999") is None
    assert coordinator.apply_event("zone_open", id="not-a-number") is None
    assert coordinator.apply_event("arm") is None
    assert coordinator.apply_event("alarm", area="999") is None
    assert coordinator.apply_event("output", id="999", state="1") is None
    assert coordinator.apply_event("bogus_event", id="1") is None


async def test_apply_event_without_snapshot_returns_none(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_local_client: AsyncMock,
    mock_local_config: Local6004Config,
) -> None:
    """With no snapshot yet there is nothing to patch."""
    mock_config_entry.add_to_hass(hass)
    coordinator = InimDataUpdateCoordinator(
        hass, mock_config_entry, mock_local_client, mock_local_config
    )
    assert coordinator.apply_event("zone_open", id="1") is None


# ---------------------------------------------------------------------------
# End to end through the HA test client
# ---------------------------------------------------------------------------
async def test_webhook_zone_open_updates_entity_without_a_refresh(
    hass: HomeAssistant,
    push_entry: MockConfigEntry,
    patch_client: AsyncMock,
    hass_client_no_auth,
) -> None:
    """The patch shows at once and is not undone by an immediate re-read.

    The panel still reports zone 1 closed here: the webhook must not trigger a
    refresh that would read that and flap the entity back off; the next
    scheduled poll reconciles.
    """
    await _setup(hass, push_entry)
    assert hass.states.get(ZONE_ENTITY).state == "off"
    reads = patch_client.async_get_zone_statuses.await_count

    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}?ev=zone_open&id=1")
    assert resp.status == 200
    await hass.async_block_till_done()

    assert hass.states.get(ZONE_ENTITY).state == "on"
    assert patch_client.async_get_zone_statuses.await_count == reads


async def test_webhook_patch_reconciled_by_the_next_poll(
    hass: HomeAssistant,
    push_entry: MockConfigEntry,
    patch_client: AsyncMock,
    zone_statuses: dict[int, NativeZoneStatus],
    hass_client_no_auth,
) -> None:
    """The next poll replaces the patch with the panel's own state."""
    coordinator = await _setup(hass, push_entry)
    client = await hass_client_no_auth()
    await client.get(f"/api/webhook/{WEBHOOK_ID}?ev=zone_open&id=1")
    await hass.async_block_till_done()
    assert hass.states.get(ZONE_ENTITY).state == "on"

    zone_statuses[1] = NativeZoneStatus(ZoneState.READY, excluded=False, alarm_memory=False)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(ZONE_ENTITY).state == "off"


async def test_webhook_body_form_encoded(
    hass: HomeAssistant,
    push_entry: MockConfigEntry,
    patch_client: AsyncMock,
    hass_client_no_auth,
) -> None:
    """Params can arrive in the form body, not just the query string."""
    await _setup(hass, push_entry)
    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}", data={"ev": "zone_open", "id": "1"})
    assert resp.status == 200
    await hass.async_block_till_done()
    assert hass.states.get(ZONE_ENTITY).state == "on"


async def test_webhook_ignores_unknown_and_malformed_calls(
    hass: HomeAssistant,
    push_entry: MockConfigEntry,
    patch_client: AsyncMock,
    hass_client_no_auth,
) -> None:
    """Unknown events, empty or junk bodies and unpatchable events answer 200."""
    await _setup(hass, push_entry)
    client = await hass_client_no_auth()

    for url, kwargs in (
        (f"/api/webhook/{WEBHOOK_ID}?ev=teleport&id=1", {}),
        (f"/api/webhook/{WEBHOOK_ID}", {}),
        (
            f"/api/webhook/{WEBHOOK_ID}",
            {
                "data": b"\x00\x01\x02 not form data",
                "headers": {"Content-Type": "application/octet-stream"},
            },
        ),
        (f"/api/webhook/{WEBHOOK_ID}?ev=zone_open&id=999", {}),
        ("/api/webhook/inim_wrong_secret?ev=zone_open&id=1", {}),
    ):
        resp = await client.post(url, **kwargs)
        assert resp.status == 200
    await hass.async_block_till_done()
    assert hass.states.get(ZONE_ENTITY).state == "off"


async def test_webhook_unregistered_after_unload(
    hass: HomeAssistant,
    push_entry: MockConfigEntry,
    patch_client: AsyncMock,
) -> None:
    """Unloading the entry unregisters the webhook (the id becomes free)."""
    await _setup(hass, push_entry)

    assert await hass.config_entries.async_unload(push_entry.entry_id)
    await hass.async_block_till_done()

    webhook.async_register(hass, "test", "probe", WEBHOOK_ID, lambda *a: None)
    webhook.async_unregister(hass, WEBHOOK_ID)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def test_register_webhook_noop_without_id(hass: HomeAssistant) -> None:
    """With no webhook id configured, register/unregister do nothing."""
    entry = MockConfigEntry(domain=DOMAIN, title="x", data={}, options={})
    webhook_mod.async_register_webhook(hass, entry)
    webhook_mod.async_unregister_webhook(hass, entry)


class _FakeRequest:
    """Minimal web.Request stand-in for the parsing helpers."""

    def __init__(self, method="POST", query=None, raw=b"", post_raises=False):
        self.method = method
        self.query = query or {}
        self._raw = raw
        self._post_raises = post_raises

    async def read(self):
        return self._raw

    async def post(self):
        if self._post_raises:
            raise ValueError("not form data")
        return {"id": "7", "file": object()}


async def test_handler_swallows_processing_errors(hass: HomeAssistant) -> None:
    """The handler answers 200 even when processing raises."""
    entry = MockConfigEntry(domain=DOMAIN, title="x")  # no runtime_data: _process raises
    handler = webhook_mod._make_handler(entry)

    resp = await handler(hass, "wid", _FakeRequest(method="GET", query={"ev": "zone_open"}))
    assert resp.status == 200


async def test_read_params_merges_body_and_query() -> None:
    """Body params are read (strings only); the query string wins on conflict."""
    request = _FakeRequest(query={"ev": "zone_open", "id": "1"}, raw=b"id=7")
    assert await webhook_mod._read_params(request) == {"ev": "zone_open", "id": "1"}
    request = _FakeRequest(query={"ev": "zone_open"}, raw=b"id=7")
    assert await webhook_mod._read_params(request) == {"ev": "zone_open", "id": "7"}


async def test_read_params_drops_oversized_or_unparsable_bodies() -> None:
    """An oversized or unparsable body is ignored; the query survives."""
    big = _FakeRequest(query={"ev": "arm"}, raw=b"x" * (MAX_WEBHOOK_BODY + 1))
    assert await webhook_mod._read_params(big) == {"ev": "arm"}
    junk = _FakeRequest(query={"ev": "arm"}, raw=b"garbage", post_raises=True)
    assert await webhook_mod._read_params(junk) == {"ev": "arm"}
