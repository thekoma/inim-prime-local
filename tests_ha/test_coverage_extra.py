"""Coverage-completing tests for the HA entities, diagnostics, webhook,
coordinator adaptive paths, config-flow branches and entity None-fallbacks.

These assert real behavior on the branches the primary test suite leaves
uncovered (error translation, missing-model fallbacks, optimistic edge cases).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import (
    ApiStatus,
    Area,
    AreaMode,
    AreaState,
    Fault,
    InimApiError,
    InimConnectionError,
    Output,
    Scenario,
    Version,
    Zone,
    ZoneState,
)
from custom_components.inim_prime.alarm_control_panel import InimAlarmControlPanel
from custom_components.inim_prime.binary_sensor import (
    InimZoneBinarySensor,
    InimAreaAlarmMemoryBinarySensor,
    InimScenarioBinarySensor,
)
from custom_components.inim_prime.button import InimClearAlarmMemoryButton
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
from custom_components.inim_prime.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.inim_prime.select import InimScenarioSelect
from custom_components.inim_prime.switch import InimOutputSwitch, InimZoneBypassSwitch
from custom_components.inim_prime import webhook as webhook_mod


VERSION = Version(version="4.07", verhttp="1.0", primex="4.07 PX500", servizio=False)


def _data(**overrides) -> InimData:
    base = dict(
        version=VERSION,
        areas=[Area(id=1, label="Home", mode=AreaMode.DISARMED, state=AreaState.READY, alarm_memory=False)],
        zones=[Zone(id=1, label="Front Door", terminal=1, state=ZoneState.READY, alarm_memory=False, excluded=False)],
        scenarios=[Scenario(id=1, label="Away", active=False)],
        outputs=[Output(id=1, label="Siren", terminal=1, state=0, type=0)],
        fault=Fault(vcc=13.7, raw_fau="0", has_faults=False),
        api_stats=None,
    )
    base.update(overrides)
    return InimData(**base)


def _fake_coordinator(data: InimData, client=None) -> SimpleNamespace:
    """A coordinator stand-in sufficient for CoordinatorEntity + our code."""
    return SimpleNamespace(
        data=data,
        client=client or AsyncMock(),
        last_update_success=True,
        async_request_refresh=AsyncMock(),
        async_add_listener=lambda *a, **k: (lambda: None),
        config_entry=SimpleNamespace(entry_id="abc123", title="INIM Prime"),
    )


# ---------------------------------------------------------------------------
# Alarm panel — None-model fallbacks and error translation
# ---------------------------------------------------------------------------
def test_alarm_state_and_attrs_when_area_missing() -> None:
    """When the backing area disappears, alarm_state is None and attrs minimal."""
    coordinator = _fake_coordinator(_data(areas=[]))
    entity = InimAlarmControlPanel(coordinator, 99)
    assert entity.alarm_state is None
    assert entity.extra_state_attributes == {"area_id": 99}
    assert entity.available is False


async def test_alarm_arm_zones_not_ready_raises_specific_error() -> None:
    """ZONES_NOT_READY from the panel maps to the zones_not_ready message."""
    client = AsyncMock()
    client.arm_area.side_effect = InimApiError(ApiStatus.ZONES_NOT_READY)
    coordinator = _fake_coordinator(_data(), client)
    entity = InimAlarmControlPanel(coordinator, 1)

    with pytest.raises(HomeAssistantError) as exc:
        await entity.async_alarm_arm_away()
    assert exc.value.translation_key == "zones_not_ready"
    # No optimistic state was applied and no refresh requested on failure.
    coordinator.async_request_refresh.assert_not_awaited()


async def test_alarm_arm_connection_error_maps_command_failed() -> None:
    """A transport failure on arm maps to the generic command_failed message."""
    client = AsyncMock()
    client.arm_area.side_effect = InimConnectionError("boom")
    entity = InimAlarmControlPanel(_fake_coordinator(_data(), client), 1)
    with pytest.raises(HomeAssistantError) as exc:
        await entity.async_alarm_arm_home()
    assert exc.value.translation_key == "command_failed"


async def test_alarm_disarm_error_maps_command_failed() -> None:
    """A non-ZONES_NOT_READY api error on disarm maps to command_failed."""
    client = AsyncMock()
    client.disarm_area.side_effect = InimApiError(ApiStatus.NOT_IMPLEMENTED)
    entity = InimAlarmControlPanel(_fake_coordinator(_data(), client), 1)
    with pytest.raises(HomeAssistantError) as exc:
        await entity.async_alarm_disarm()
    assert exc.value.translation_key == "command_failed"


# ---------------------------------------------------------------------------
# Switch — None-model fallbacks and error translation
# ---------------------------------------------------------------------------
def test_output_switch_none_when_output_missing() -> None:
    """A missing output reports is_on None, name None, unavailable."""
    coordinator = _fake_coordinator(_data(outputs=[]))
    entry = SimpleNamespace(entry_id="abc123", title="INIM Prime")
    entity = InimOutputSwitch(coordinator, entry, 99)
    assert entity.is_on is None
    assert entity.name is None
    assert entity.available is False


async def test_output_switch_code_not_allowed() -> None:
    """CODE_NOT_ALLOWED maps to the output_code_not_allowed message."""
    client = AsyncMock()
    client.set_output.side_effect = InimApiError(ApiStatus.CODE_NOT_ALLOWED)
    coordinator = _fake_coordinator(_data(), client)
    entry = SimpleNamespace(entry_id="abc123", title="INIM Prime")
    entity = InimOutputSwitch(coordinator, entry, 1)
    with pytest.raises(HomeAssistantError) as exc:
        await entity.async_turn_on()
    assert exc.value.translation_key == "output_code_not_allowed"


async def test_output_switch_other_api_error_command_failed() -> None:
    """A non-CODE_NOT_ALLOWED api error maps to command_failed."""
    client = AsyncMock()
    client.set_output.side_effect = InimApiError(ApiStatus.NOT_IMPLEMENTED)
    coordinator = _fake_coordinator(_data(), client)
    entry = SimpleNamespace(entry_id="abc123", title="INIM Prime")
    entity = InimOutputSwitch(coordinator, entry, 1)
    with pytest.raises(HomeAssistantError) as exc:
        await entity.async_turn_off()
    assert exc.value.translation_key == "command_failed"


async def test_output_switch_connection_error_command_failed() -> None:
    """A transport failure on set_output maps to command_failed."""
    client = AsyncMock()
    client.set_output.side_effect = InimConnectionError("down")
    coordinator = _fake_coordinator(_data(), client)
    entry = SimpleNamespace(entry_id="abc123", title="INIM Prime")
    entity = InimOutputSwitch(coordinator, entry, 1)
    with pytest.raises(HomeAssistantError) as exc:
        await entity.async_turn_on()
    assert exc.value.translation_key == "command_failed"


def test_zone_bypass_none_when_zone_missing() -> None:
    """A missing zone reports is_on None and name None."""
    coordinator = _fake_coordinator(_data(zones=[]))
    entry = SimpleNamespace(entry_id="abc123", title="INIM Prime")
    entity = InimZoneBypassSwitch(coordinator, entry, 99)
    assert entity.is_on is None
    assert entity.name is None


async def test_zone_bypass_error_maps_command_failed() -> None:
    """A panel failure on set_zone_excluded maps to command_failed."""
    client = AsyncMock()
    client.set_zone_excluded.side_effect = InimConnectionError("down")
    coordinator = _fake_coordinator(_data(), client)
    entry = SimpleNamespace(entry_id="abc123", title="INIM Prime")
    entity = InimZoneBypassSwitch(coordinator, entry, 1)
    with pytest.raises(HomeAssistantError) as exc:
        await entity.async_turn_on()
    assert exc.value.translation_key == "command_failed"


# ---------------------------------------------------------------------------
# Select — error translation branches
# ---------------------------------------------------------------------------
async def test_select_zones_not_ready() -> None:
    """Applying a scenario that returns ZONES_NOT_READY maps to that message."""
    client = AsyncMock()
    client.apply_scenario.side_effect = InimApiError(ApiStatus.ZONES_NOT_READY)
    coordinator = _fake_coordinator(_data())
    coordinator.client = client
    entity = InimScenarioSelect(coordinator)
    with pytest.raises(HomeAssistantError) as exc:
        await entity.async_select_option("Away")
    assert exc.value.translation_key == "zones_not_ready"
    coordinator.async_request_refresh.assert_not_awaited()


async def test_select_other_api_error_command_failed() -> None:
    """A non-ZONES_NOT_READY api error maps to command_failed."""
    client = AsyncMock()
    client.apply_scenario.side_effect = InimApiError(ApiStatus.NOT_IMPLEMENTED)
    coordinator = _fake_coordinator(_data())
    coordinator.client = client
    entity = InimScenarioSelect(coordinator)
    with pytest.raises(HomeAssistantError) as exc:
        await entity.async_select_option("Away")
    assert exc.value.translation_key == "command_failed"


async def test_select_connection_error_command_failed() -> None:
    """A transport failure maps to command_failed."""
    client = AsyncMock()
    client.apply_scenario.side_effect = InimConnectionError("down")
    coordinator = _fake_coordinator(_data())
    coordinator.client = client
    entity = InimScenarioSelect(coordinator)
    with pytest.raises(HomeAssistantError) as exc:
        await entity.async_select_option("Away")
    assert exc.value.translation_key == "command_failed"


# ---------------------------------------------------------------------------
# Button — error translation
# ---------------------------------------------------------------------------
async def test_button_press_error_maps_command_failed() -> None:
    """A failed clear_alarm_memory write maps to command_failed."""
    client = AsyncMock()
    client.clear_alarm_memory.side_effect = InimApiError(ApiStatus.NOT_IMPLEMENTED)
    coordinator = _fake_coordinator(_data(), client)
    area = coordinator.data.areas[0]
    entity = InimClearAlarmMemoryButton(coordinator, area)
    with pytest.raises(HomeAssistantError) as exc:
        await entity.async_press()
    assert exc.value.translation_key == "command_failed"
    coordinator.async_request_refresh.assert_not_awaited()


# ---------------------------------------------------------------------------
# Binary sensors — None-model fallbacks
# ---------------------------------------------------------------------------
def test_binary_sensor_none_fallbacks() -> None:
    """Each binary sensor returns None when its backing model is absent."""
    coordinator = _fake_coordinator(_data(zones=[], areas=[], scenarios=[]))

    zone_bs = InimZoneBinarySensor(coordinator, 99)
    assert zone_bs.is_on is None
    assert zone_bs.extra_state_attributes is None

    area_bs = InimAreaAlarmMemoryBinarySensor(coordinator, 99)
    assert area_bs.is_on is None

    scen_bs = InimScenarioBinarySensor(coordinator, 99)
    assert scen_bs.is_on is None


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
async def test_diagnostics_redacts_secrets(hass: HomeAssistant) -> None:
    """Diagnostics redact the apikey/webhook id and dump coordinator data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="INIM Prime",
        data={
            CONF_HOST: "192.0.2.10",
            CONF_PORT: 8080,
            CONF_APIKEY: "secret-key",
            CONF_USE_HTTPS: False,
        },
        options={CONF_WEBHOOK_ID: "topsecret"},
        unique_id="192.0.2.10:8080",
    )
    coordinator = _fake_coordinator(_data())
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"][CONF_APIKEY] == "**REDACTED**"
    assert diag["options"][CONF_WEBHOOK_ID] == "**REDACTED**"
    assert diag["entry"][CONF_HOST] == "192.0.2.10"
    # The data section is an asdict of the InimData snapshot.
    assert diag["data"]["version"]["primex"] == "4.07 PX500"
    assert diag["data"]["areas"][0]["label"] == "Home"


async def test_diagnostics_handles_no_data(hass: HomeAssistant) -> None:
    """With no coordinator snapshot yet, the data section is None."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="INIM Prime",
        data={CONF_HOST: "192.0.2.10", CONF_PORT: 8080, CONF_APIKEY: "k"},
        unique_id="192.0.2.10:8080",
    )
    coordinator = SimpleNamespace(data=None)
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["data"] is None


# ---------------------------------------------------------------------------
# Webhook helpers — no-op register branch
# ---------------------------------------------------------------------------
def test_register_webhook_noop_without_id(hass: HomeAssistant) -> None:
    """With no webhook id configured, register/unregister are no-ops."""
    entry = MockConfigEntry(domain=DOMAIN, title="x", data={}, options={})
    # Neither should raise nor register anything.
    webhook_mod.async_register_webhook(hass, entry)
    webhook_mod.async_unregister_webhook(hass, entry)


class _FakeRequest:
    """Minimal web.Request stand-in for _read_params/_process tests."""

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
        return {}


async def test_webhook_handler_swallows_processing_errors(hass: HomeAssistant) -> None:
    """The handler always returns 200 even if _process raises."""
    entry = SimpleNamespace()
    # runtime_data is absent -> _process raises AttributeError, which the
    # handler must swallow and still answer 200.
    handler = webhook_mod._make_handler(entry)

    request = _FakeRequest(method="GET", query={"ev": "zone_open", "id": "1"})
    resp = await handler(hass, "wid", request)
    assert resp.status == 200


async def test_read_params_body_too_large_ignored() -> None:
    """An oversized POST body is ignored, leaving only query params."""
    from custom_components.inim_prime.const import MAX_WEBHOOK_BODY

    request = _FakeRequest(
        method="POST",
        query={"ev": "zone_open"},
        raw=b"x" * (MAX_WEBHOOK_BODY + 1),
    )
    params = await webhook_mod._read_params(request)
    # Body dropped; query survives.
    assert params == {"ev": "zone_open"}


async def test_read_params_post_parse_failure_yields_empty_body() -> None:
    """If request.post() raises, the body params fall back to empty."""
    request = _FakeRequest(method="POST", query={"ev": "arm"}, raw=b"garbage", post_raises=True)
    params = await webhook_mod._read_params(request)
    assert params == {"ev": "arm"}


# ---------------------------------------------------------------------------
# Coordinator — adaptive paths and apply_event edge branches
# ---------------------------------------------------------------------------
def _push_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="INIM Prime",
        data={CONF_HOST: "192.0.2.10", CONF_PORT: 8080, CONF_APIKEY: "k", CONF_USE_HTTPS: False, "scan_interval": 15},
        options={
            CONF_WEBHOOK_ENABLED: True,
            CONF_WEBHOOK_ID: "secret",
            CONF_SCAN_INTERVAL_IDLE: 30,
            CONF_SCAN_INTERVAL_ACTIVE: 3,
        },
        unique_id="192.0.2.10:8080",
    )


async def test_activate_fast_poll_twice_cancels_prior_decay(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """Re-activating fast poll cancels the previously-scheduled decay timer."""
    entry = _push_entry()
    entry.add_to_hass(hass)
    coordinator = InimDataUpdateCoordinator(hass, entry, mock_client)

    coordinator.activate_fast_poll()
    first_cancel = coordinator._cancel_decay
    assert first_cancel is not None

    # Second activation must cancel the first decay handle and schedule anew.
    coordinator.activate_fast_poll()
    assert coordinator._cancel_decay is not None
    assert coordinator._cancel_decay is not first_cancel


async def test_apply_event_alarm_missing_area_returns_none(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """An alarm event for an unknown area returns None (poll reconciles)."""
    mock_config_entry.add_to_hass(hass)
    coordinator = InimDataUpdateCoordinator(hass, mock_config_entry, mock_client)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.apply_event("alarm", area="999") is None


async def test_apply_event_output_missing_and_bad_state(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """Output event: unknown id -> None; bad state string -> defaults to on (1)."""
    mock_config_entry.add_to_hass(hass)
    coordinator = InimDataUpdateCoordinator(hass, mock_config_entry, mock_client)
    coordinator.data = await coordinator._async_update_data()

    # Unknown output id reconciles to None.
    assert coordinator.apply_event("output", id="999", state="1") is None

    # A non-integer state falls back to 1 (on) rather than crashing.
    patched = coordinator.apply_event("output", id="1", state="not-a-number")
    assert patched is not None
    assert patched.outputs[0].state == 1


# ---------------------------------------------------------------------------
# Config flow — reconfigure collision + options disabling push mode
# ---------------------------------------------------------------------------
async def test_user_flow_unexpected_exception_is_unknown(hass: HomeAssistant) -> None:
    """A non-Inim exception during validation surfaces the 'unknown' error."""
    from unittest.mock import patch

    client = AsyncMock()
    client.version.side_effect = RuntimeError("totally unexpected")
    with patch(
        "custom_components.inim_prime.config_flow.InimPrimeClient",
        return_value=client,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "192.0.2.55",
                CONF_PORT: 8080,
                CONF_APIKEY: "k",
                CONF_USE_HTTPS: False,
            },
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_reconfigure_unique_id_collision_aborts(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """Reconfiguring onto another entry's host:port aborts as already_configured."""
    # The entry being reconfigured.
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="192.0.2.10",
        data={CONF_HOST: "192.0.2.10", CONF_PORT: 8080, CONF_APIKEY: "k", CONF_USE_HTTPS: False},
        unique_id="192.0.2.10:8080",
    )
    entry.add_to_hass(hass)
    # A different, pre-existing entry occupying the target unique id.
    other = MockConfigEntry(
        domain=DOMAIN,
        title="other",
        data={CONF_HOST: "192.0.2.99", CONF_PORT: 9090, CONF_APIKEY: "k2", CONF_USE_HTTPS: False},
        unique_id="192.0.2.99:9090",
    )
    other.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "192.0.2.99",
            CONF_PORT: 9090,
            CONF_APIKEY: "k",
            CONF_USE_HTTPS: False,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_disable_push_drops_webhook_id(
    hass: HomeAssistant, mock_client: AsyncMock, patch_client: AsyncMock
) -> None:
    """Turning push mode off removes the stored webhook id from options."""
    entry = _push_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.options[CONF_WEBHOOK_ID] == "secret"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL_IDLE: 30,
            CONF_SCAN_INTERVAL_ACTIVE: 3,
            CONF_WEBHOOK_ENABLED: False,
        },
    )
    await hass.async_block_till_done()

    assert entry.options[CONF_WEBHOOK_ENABLED] is False
    assert CONF_WEBHOOK_ID not in entry.options
