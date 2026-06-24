"""Tests for the INIM Prime config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock

from custom_components.inim_prime.client import InimApiError, InimConnectionError, ApiStatus

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.const import (
    CONF_APIKEY,
    CONF_SCAN_INTERVAL_ACTIVE,
    CONF_SCAN_INTERVAL_IDLE,
    CONF_USE_HTTPS,
    DEFAULT_SCAN_INTERVAL_ACTIVE,
    DEFAULT_SCAN_INTERVAL_IDLE,
    DOMAIN,
)

USER_INPUT = {
    CONF_HOST: "192.0.2.10",
    CONF_PORT: 8080,
    CONF_APIKEY: "secret-key",
    CONF_USE_HTTPS: False,
}


async def test_user_flow_success(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """A valid config creates an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    # The add-hub form must NOT offer a scan_interval field.
    assert CONF_SCAN_INTERVAL not in result["data_schema"].schema

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "192.0.2.10"
    assert result["data"] == USER_INPUT
    # No stale scan_interval is persisted into the entry.
    assert CONF_SCAN_INTERVAL not in result["data"]
    assert result["result"].unique_id == "192.0.2.10:8080"
    patch_client.version.assert_awaited()


async def test_user_flow_invalid_auth(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """An invalid API key surfaces invalid_auth and allows recovery."""
    patch_client.version.side_effect = InimApiError(ApiStatus.ERROR_APIKEY)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    # Recover with a working key.
    patch_client.version.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_cannot_connect(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """A connection failure surfaces cannot_connect."""
    patch_client.version.side_effect = InimConnectionError("boom")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_other_api_error_is_unknown(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """A non-apikey API error surfaces unknown."""
    patch_client.version.side_effect = InimApiError(ApiStatus.ERROR_COMMAND)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


def _add_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create and register a config entry to reconfigure."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="192.0.2.10",
        data=dict(USER_INPUT),
        unique_id="192.0.2.10:8080",
    )
    entry.add_to_hass(hass)
    return entry


async def test_reconfigure_success(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """Reconfigure updates apikey (and host) and reloads the entry."""
    entry = _add_entry(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    # The reconfigure form is for credentials only, not polling tuning.
    assert CONF_SCAN_INTERVAL not in result["data_schema"].schema

    new_input = {
        CONF_HOST: "192.0.2.20",
        CONF_PORT: 8080,
        CONF_APIKEY: "rotated-key",
        CONF_USE_HTTPS: True,
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], new_input
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_APIKEY] == "rotated-key"
    assert entry.data[CONF_HOST] == "192.0.2.20"
    assert entry.data[CONF_USE_HTTPS] is True
    # unique_id stays consistent with host:port.
    assert entry.unique_id == "192.0.2.20:8080"
    patch_client.version.assert_awaited()


async def test_reconfigure_invalid_auth_then_recovers(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """A bad new apikey surfaces invalid_auth, then recovery succeeds."""
    entry = _add_entry(hass)
    patch_client.version.side_effect = InimApiError(ApiStatus.ERROR_APIKEY)

    result = await entry.start_reconfigure_flow(hass)
    new_input = {
        CONF_HOST: "192.0.2.10",
        CONF_PORT: 8080,
        CONF_APIKEY: "bad-key",
        CONF_USE_HTTPS: False,
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], new_input
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    # Recover with a good key.
    patch_client.version.side_effect = None
    new_input[CONF_APIKEY] = "good-key"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], new_input
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_APIKEY] == "good-key"


async def test_reconfigure_cannot_connect(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """A connection failure during reconfigure surfaces cannot_connect."""
    entry = _add_entry(hass)
    patch_client.version.side_effect = InimConnectionError("boom")

    result = await entry.start_reconfigure_flow(hass)
    new_input = {
        CONF_HOST: "192.0.2.10",
        CONF_PORT: 8080,
        CONF_APIKEY: "secret-key",
        CONF_USE_HTTPS: False,
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], new_input
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_success(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """Reauth re-enters the API key, updates the entry and reloads."""
    entry = _add_entry(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    # Only the API key is editable in reauth; host/port are not offered.
    schema = result["data_schema"].schema
    assert CONF_HOST not in schema
    assert CONF_PORT not in schema
    assert CONF_APIKEY in schema

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_APIKEY: "rotated-key"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_APIKEY] == "rotated-key"
    # Host/port preserved.
    assert entry.data[CONF_HOST] == "192.0.2.10"
    assert entry.data[CONF_PORT] == 8080
    patch_client.version.assert_awaited()


async def test_reauth_invalid_auth(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """A still-invalid key during reauth surfaces invalid_auth, then recovers."""
    entry = _add_entry(hass)
    patch_client.version.side_effect = InimApiError(ApiStatus.ERROR_APIKEY)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_APIKEY: "still-bad"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    patch_client.version.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_APIKEY: "good-key"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_APIKEY] == "good-key"


async def test_reauth_cannot_connect(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """A connection failure during reauth surfaces cannot_connect."""
    entry = _add_entry(hass)
    patch_client.version.side_effect = InimConnectionError("boom")

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_APIKEY: "secret-key"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_options_poll_interval_defaults(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """The options form defaults idle to 30 and active to 1 (not 15)."""
    entry = _add_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    schema = result["data_schema"].schema
    # The legacy single scan_interval must be gone from options too.
    assert CONF_SCAN_INTERVAL not in schema

    defaults = {
        key.schema: key.default()
        for key in schema
        if hasattr(key, "default") and key.default is not None
    }
    assert defaults[CONF_SCAN_INTERVAL_IDLE] == DEFAULT_SCAN_INTERVAL_IDLE == 30
    assert defaults[CONF_SCAN_INTERVAL_ACTIVE] == DEFAULT_SCAN_INTERVAL_ACTIVE == 1
