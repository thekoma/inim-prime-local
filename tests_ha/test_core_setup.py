"""Tests for the INIM Prime entry setup, migration, unload and diagnostics."""

from __future__ import annotations

import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import (
    Local6004Config,
    Local6004Error,
    Local6004Structure,
)
from custom_components.inim_prime.const import (
    CONF_LOCAL_PASSWORD,
    CONF_WEBHOOK_ENABLED,
    CONF_WEBHOOK_ID,
    DOMAIN,
)
from custom_components.inim_prime.diagnostics import async_get_config_entry_diagnostics


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    entry.add_to_hass(hass)
    result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return result


async def test_setup_loads_and_unload_closes_the_client(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """A readable panel loads; unloading closes the native connection."""
    assert await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data.client is patch_client
    patch_client.async_read_config.assert_awaited_once()

    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, mock_config_entry.entry_id)}
    )
    assert device is not None
    assert (device.manufacturer, device.model, device.sw_version) == ("INIM", "PX020", "4.07")

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    patch_client.async_close.assert_awaited()


async def test_setup_without_password_fails(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """An entry without the LAN password cannot be set up."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={"host": "192.0.2.10"}, unique_id="192.0.2.10", version=2
    )

    assert not await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    patch_client.async_read_config.assert_not_awaited()


async def test_setup_takes_a_password_from_the_options(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """A password stored in the options (older entries) is used too."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "192.0.2.10"},
        options={CONF_LOCAL_PASSWORD: "pass"},
        unique_id="192.0.2.10",
        version=2,
    )

    assert await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED


async def test_setup_retries_while_the_panel_is_unreachable(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """A config read failure is retried by Home Assistant."""
    patch_client.async_read_config.side_effect = Local6004Error("refused")

    assert not await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_retries_when_the_first_reading_fails(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """A failing first live reading is retried, not loaded half-empty."""
    patch_client.async_get_area_statuses.side_effect = Local6004Error("timeout")

    assert not await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_refuses_an_unsupported_firmware(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """A firmware whose memory map is not known is a permanent setup error."""
    patch_client.async_read_config.return_value = Local6004Config(
        firmware="6.00 PX020", layout_ok=False
    )

    assert not await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR


@pytest.mark.parametrize(
    "structure",
    [
        None,
        Local6004Structure(areas=None, zones=[]),
        Local6004Structure(areas=[], zones=None),
    ],
)
async def test_setup_retries_without_areas_or_zones(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    mock_local_config: Local6004Config,
    structure: Local6004Structure | None,
) -> None:
    """Areas and zones are required: setup retries until they are read."""
    patch_client.async_read_config.return_value = replace(mock_local_config, structure=structure)

    assert not await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_warns_without_scenarios_or_outputs(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    patch_client: AsyncMock,
    mock_local_config: Local6004Config,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scenarios and outputs are optional: setup loads and logs what is missing."""
    patch_client.async_read_config.return_value = replace(
        mock_local_config,
        structure=replace(mock_local_config.structure, scenarios=None, outputs=None),
    )

    with caplog.at_level(logging.WARNING):
        assert await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("panel scenarios" in m for m in messages)
    assert any("panel outputs" in m for m in messages)
    data = mock_config_entry.runtime_data.coordinator.data
    assert (data.scenarios, data.outputs) == ([], [])


async def test_setup_removes_the_cgi_only_sensors(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """The API version and connection sensors of earlier versions are removed."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy = [
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{mock_config_entry.entry_id}_sensor_{key}",
            config_entry=mock_config_entry,
        ).entity_id
        for key in ("api_version", "api_connections", "api_last_ip")
    ]

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    for entity_id in legacy:
        assert registry.async_get(entity_id) is None
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, f"{mock_config_entry.entry_id}_sensor_supply_voltage"
    )


async def test_migration_drops_the_cgi_settings(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """A version 1 entry loses its cgi keys and gets the host as unique id."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="INIM Prime",
        data={
            "host": "192.0.2.10",
            "port": 8080,
            "apikey": "secret-key",
            "use_https": False,
            "scan_interval": 15,
            CONF_LOCAL_PASSWORD: "pass",
        },
        options={
            "scan_interval": 15,
            "scan_interval_idle": 30,
            "scan_interval_active": 1,
            "native_area_poll": True,
            "native_commands": True,
            CONF_WEBHOOK_ENABLED: False,
        },
        unique_id="192.0.2.10:8080",
        version=1,
    )

    assert await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 2
    assert entry.unique_id == "192.0.2.10"
    assert dict(entry.data) == {"host": "192.0.2.10", CONF_LOCAL_PASSWORD: "pass"}
    assert dict(entry.options) == {CONF_WEBHOOK_ENABLED: False}


async def test_migration_refuses_a_newer_entry(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """An entry from a newer version (downgrade) is not loaded."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "192.0.2.10", CONF_LOCAL_PASSWORD: "pass"},
        unique_id="192.0.2.10",
        version=3,
    )

    assert not await _setup(hass, entry)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
async def test_diagnostics_redact_the_secrets(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """The LAN password and the webhook id never appear in diagnostics."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="INIM Prime",
        data={"host": "192.0.2.10", CONF_LOCAL_PASSWORD: "lan-secret"},
        options={CONF_WEBHOOK_ENABLED: True, CONF_WEBHOOK_ID: "hook-secret"},
        unique_id="192.0.2.10",
        version=2,
    )
    assert await _setup(hass, entry)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry"] == {"host": "192.0.2.10", CONF_LOCAL_PASSWORD: "**REDACTED**"}
    assert diag["options"][CONF_WEBHOOK_ID] == "**REDACTED**"
    assert "lan-secret" not in repr(diag) and "hook-secret" not in repr(diag)
    assert diag["data"]["version"]["raw"] == "4.07 PX020"
    assert diag["data"]["areas"][0]["label"] == "Home"


async def test_diagnostics_without_a_snapshot(hass: HomeAssistant) -> None:
    """With no snapshot yet, the data section is None."""
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "192.0.2.10"}, version=2)
    entry.runtime_data = SimpleNamespace(coordinator=SimpleNamespace(data=None))

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["data"] is None


def test_room_device_info_links_to_the_panel() -> None:
    """A room device gets an accent-folded id and hangs off the panel device."""
    from custom_components.inim_prime.device import room_device_info

    coordinator = SimpleNamespace(config_entry=SimpleNamespace(entry_id="abc"))

    info = room_device_info(coordinator, "Café  Room")

    assert info["identifiers"] == {(DOMAIN, "abc_room_cafe_room")}
    assert info["name"] == "Café  Room"
    assert info["via_device"] == (DOMAIN, "abc")
