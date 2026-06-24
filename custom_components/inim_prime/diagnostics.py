"""Diagnostics support for the INIM Prime integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_APIKEY, CONF_WEBHOOK_ID
from .coordinator import InimConfigEntry

TO_REDACT = {CONF_APIKEY, CONF_WEBHOOK_ID}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: InimConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator
    data = asdict(coordinator.data) if coordinator.data is not None else None

    return {
        "entry": async_redact_data(entry.data, TO_REDACT),
        "options": async_redact_data(entry.options, TO_REDACT),
        "data": data,
    }
