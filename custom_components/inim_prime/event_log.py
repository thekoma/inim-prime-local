"""Event-log action for the INIM Prime integration.

Registers ``inim_prime.get_event_log``: a strictly READ-ONLY, on-demand action
that reads the panel's stored event log over the local protocol (TCP 6004),
decodes it (time, event, partitions, scenario, restoral) and returns it as the
service response. Nothing is stored in Home Assistant — a card or script calls
this when it wants the history.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .client import Local6004Error
from .const import DOMAIN
from .coordinator import InimConfigEntry

SERVICE_GET_EVENT_LOG = "get_event_log"
ATTR_LIMIT = "limit"

GET_EVENT_LOG_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional(ATTR_LIMIT, default=100): vol.All(int, vol.Range(min=0)),
    }
)


def _resolve_entry(hass: HomeAssistant, entity_id: str) -> InimConfigEntry:
    """Find the INIM config entry that owns ``entity_id``."""
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None or entry.config_entry_id is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="event_log_bad_target",
            translation_placeholders={"entity_id": entity_id},
        )
    config_entry = hass.config_entries.async_get_entry(entry.config_entry_id)
    if config_entry is None or config_entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="event_log_bad_target",
            translation_placeholders={"entity_id": entity_id},
        )
    return config_entry


async def _handle_get_event_log(call: ServiceCall) -> ServiceResponse:
    """Read and return the decoded panel event log."""
    hass = call.hass
    entity_ids: list[str] = call.data[ATTR_ENTITY_ID]
    if len(entity_ids) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="event_log_single_target"
        )
    entry = _resolve_entry(hass, entity_ids[0])
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    local_client = runtime.local_client
    if local_client is None:  # mandatory 6004 means this is set after setup
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={"error": "local channel unavailable"},
        )

    area_labels = {area.id: area.label for area in coordinator.data.areas}
    scenario_labels = {s.id: s.label for s in coordinator.data.scenarios}
    try:
        events = await local_client.async_read_event_log(area_labels, scenario_labels)
    except Local6004Error as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={"error": str(err)},
        ) from err

    limit: int = call.data[ATTR_LIMIT]
    if limit:
        events = events[-limit:]
    result: dict[str, Any] = {"count": len(events), "events": events}
    return result


def async_register_event_log_service(hass: HomeAssistant) -> None:
    """Register the read-only event-log action (once)."""
    if hass.services.has_service(DOMAIN, SERVICE_GET_EVENT_LOG):
        return
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_EVENT_LOG,
        _handle_get_event_log,
        schema=GET_EVENT_LOG_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
