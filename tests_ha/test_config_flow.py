"""Tests for the INIM Prime config flow (native only: host + LAN password)."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import Local6004Config, Local6004Error
from custom_components.inim_prime.const import (
    CONF_GROUP_BY_ROOM,
    CONF_LABEL_LANGUAGE,
    CONF_LOCAL_PASSWORD,
    CONF_REQUIRE_CODE,
    CONF_WEBHOOK_ENABLED,
    CONF_WEBHOOK_ID,
    DOMAIN,
)

USER_INPUT = {CONF_NAME: "Panel", CONF_HOST: "192.0.2.10", CONF_LOCAL_PASSWORD: "pass"}


async def _user_flow(hass: HomeAssistant, user_input: dict = USER_INPUT):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def test_user_flow_success(hass: HomeAssistant, patch_client: AsyncMock) -> None:
    """A readable panel creates an entry named after the friendly name."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert set(result["data_schema"].schema) == {CONF_NAME, CONF_HOST, CONF_LOCAL_PASSWORD}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Panel"
    assert result["data"] == USER_INPUT
    assert result["result"].unique_id == "192.0.2.10"
    assert result["result"].version == 2
    patch_client.async_read_config.assert_awaited()


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (Local6004Error("refused"), "cannot_connect"),
        (RuntimeError("unexpected"), "unknown"),
    ],
)
async def test_user_flow_errors_then_recovers(
    hass: HomeAssistant, patch_client: AsyncMock, failure: Exception, error: str
) -> None:
    """A read failure shows its error on the form, and a retry can succeed."""
    config = patch_client.async_read_config.return_value
    patch_client.async_read_config.side_effect = failure

    result = await _user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    patch_client.async_read_config.side_effect = None
    patch_client.async_read_config.return_value = config
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_unsupported_firmware(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """A panel whose firmware memory map is not known is refused."""
    patch_client.async_read_config.return_value = Local6004Config(
        firmware="6.00 PX020", layout_ok=False
    )

    result = await _user_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unsupported_firmware"}


async def test_user_flow_already_configured(hass: HomeAssistant, patch_client: AsyncMock) -> None:
    """The same host cannot be added twice."""
    MockConfigEntry(domain=DOMAIN, data=USER_INPUT, unique_id="192.0.2.10", version=2).add_to_hass(
        hass
    )

    result = await _user_flow(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    patch_client.async_read_config.assert_not_awaited()


def _add_entry(hass: HomeAssistant, **overrides) -> MockConfigEntry:
    """Register a config entry to reauthenticate or reconfigure."""
    data = {**USER_INPUT, **overrides}
    entry = MockConfigEntry(
        domain=DOMAIN, title="Panel", data=data, unique_id=data[CONF_HOST], version=2
    )
    entry.add_to_hass(hass)
    return entry


async def test_reauth_updates_the_password(hass: HomeAssistant, patch_client: AsyncMock) -> None:
    """Reauth asks only for the LAN password, validates it and stores it."""
    entry = _add_entry(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert set(result["data_schema"].schema) == {CONF_LOCAL_PASSWORD}
    assert result["description_placeholders"][CONF_HOST] == "192.0.2.10"

    patch_client.async_read_config.side_effect = Local6004Error("bad password")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCAL_PASSWORD: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data[CONF_LOCAL_PASSWORD] == "pass"

    patch_client.async_read_config.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCAL_PASSWORD: "rotated"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_LOCAL_PASSWORD] == "rotated"
    assert entry.data[CONF_HOST] == "192.0.2.10"


async def test_reconfigure_updates_name_host_and_password(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """Reconfigure moves the entry to a new host; the unique id follows."""
    entry = _add_entry(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert set(result["data_schema"].schema) == {CONF_NAME, CONF_HOST, CONF_LOCAL_PASSWORD}

    new_input = {CONF_NAME: "Alarm", CONF_HOST: "192.0.2.20", CONF_LOCAL_PASSWORD: "rotated"}
    patch_client.async_read_config.side_effect = Local6004Error("unreachable")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new_input)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    patch_client.async_read_config.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new_input)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.title == "Alarm"
    assert entry.data == new_input
    assert entry.unique_id == "192.0.2.20"


async def test_reconfigure_same_host_is_not_a_collision(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """Keeping the entry's own host is allowed."""
    entry = _add_entry(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_LOCAL_PASSWORD: "rotated"}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_LOCAL_PASSWORD] == "rotated"


async def test_reconfigure_onto_another_entry_aborts(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """Reconfiguring onto another entry's host aborts as already_configured."""
    entry = _add_entry(hass)
    _add_entry(hass, **{CONF_HOST: "192.0.2.99"})

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: "192.0.2.99"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    patch_client.async_read_config.assert_not_awaited()


async def test_reconfigure_unsupported_firmware(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """A host whose panel firmware is not supported is refused."""
    entry = _add_entry(hass)
    patch_client.async_read_config.return_value = replace(
        patch_client.async_read_config.return_value, layout_ok=False
    )

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["errors"] == {"base": "unsupported_firmware"}


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
async def _setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_options_form_has_no_polling_fields(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """Only the areas, push mode, label language, room grouping and require-code."""
    entry = _add_entry(hass)
    await _setup_entry(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert set(result["data_schema"].schema) == {
        "areas",
        CONF_WEBHOOK_ENABLED,
        CONF_LABEL_LANGUAGE,
        CONF_GROUP_BY_ROOM,
        CONF_REQUIRE_CODE,
    }


async def test_options_require_code_saved(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """Turning on 'require code' is stored in the entry options (off by default)."""
    entry = _add_entry(hass)
    await _setup_entry(hass, entry)
    assert entry.options.get(CONF_REQUIRE_CODE, False) is False

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_WEBHOOK_ENABLED: False,
            CONF_LABEL_LANGUAGE: "en",
            CONF_GROUP_BY_ROOM: True,
            CONF_REQUIRE_CODE: True,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_REQUIRE_CODE] is True


async def test_options_enable_push_generates_a_stable_webhook(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """Push mode stores a webhook id once, registers it and shows the URL."""
    entry = _add_entry(hass)
    await _setup_entry(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch("custom_components.inim_prime.config_flow.async_create_notification") as notify:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_WEBHOOK_ENABLED: True, CONF_LABEL_LANGUAGE: "en", CONF_GROUP_BY_ROOM: False},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    webhook_id = entry.options[CONF_WEBHOOK_ID]
    assert webhook_id
    assert entry.options[CONF_LABEL_LANGUAGE] == "en"
    assert entry.options[CONF_GROUP_BY_ROOM] is False
    assert webhook_id in notify.call_args.args[1]

    # Saving again keeps the same secret.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_WEBHOOK_ENABLED: True}
    )
    await hass.async_block_till_done()
    assert entry.options[CONF_WEBHOOK_ID] == webhook_id


async def test_options_disable_push_drops_the_webhook_id(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """Turning push mode off removes the stored webhook id."""
    entry = _add_entry(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_WEBHOOK_ENABLED: True, CONF_WEBHOOK_ID: "secret"}
    )
    await _setup_entry(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_WEBHOOK_ENABLED: False}
    )
    await hass.async_block_till_done()

    assert entry.options[CONF_WEBHOOK_ENABLED] is False
    assert CONF_WEBHOOK_ID not in entry.options


async def test_options_without_a_loaded_entry_offer_no_areas(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """The area list comes from the running coordinator: none while not loaded."""
    entry = _add_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert "areas" not in result["data_schema"].schema
