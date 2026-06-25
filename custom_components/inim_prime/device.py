"""Shared helpers for zone label language and per-room device grouping."""

from __future__ import annotations

import unicodedata

from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CONF_GROUP_BY_ROOM,
    CONF_LABEL_LANGUAGE,
    DEFAULT_GROUP_BY_ROOM,
    DOMAIN,
    LABEL_LANGUAGE_AUTO,
)
from .coordinator import InimDataUpdateCoordinator


def label_language(coordinator: InimDataUpdateCoordinator) -> str:
    """Resolve the language used to guess zone device classes/rooms from labels.

    Uses the per-entry override option when set, otherwise the Home Assistant
    system language (``hass.config.language``).
    """
    configured = coordinator.config_entry.options.get(
        CONF_LABEL_LANGUAGE, LABEL_LANGUAGE_AUTO
    )
    if configured and configured != LABEL_LANGUAGE_AUTO:
        return str(configured)
    return coordinator.hass.config.language


def group_zones_by_room(coordinator: InimDataUpdateCoordinator) -> bool:
    """Whether zones should be grouped under per-room devices."""
    return bool(
        coordinator.config_entry.options.get(
            CONF_GROUP_BY_ROOM, DEFAULT_GROUP_BY_ROOM
        )
    )


def _room_slug(room: str) -> str:
    """Stable identifier fragment for a room name (accent-folded, underscored)."""
    folded = "".join(
        c
        for c in unicodedata.normalize("NFKD", room.lower())
        if not unicodedata.combining(c)
    )
    return "_".join(folded.split())


def room_device_info(
    coordinator: InimDataUpdateCoordinator, room: str
) -> DeviceInfo:
    """Build the DeviceInfo for a per-room sub-device linked to the panel."""
    entry = coordinator.config_entry
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_room_{_room_slug(room)}")},
        name=room,
        manufacturer="INIM",
        via_device=(DOMAIN, entry.entry_id),
    )
