"""Shared helpers for zone label language and per-room device grouping."""

from __future__ import annotations

import unicodedata

from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CONF_GROUP_BY_ROOM,
    CONF_LABEL_LANGUAGE,
    CONF_ROOM_OVERRIDES,
    DEFAULT_GROUP_BY_ROOM,
    DEFAULT_ROOM_OVERRIDES,
    DOMAIN,
    LABEL_LANGUAGE_AUTO,
)
from .coordinator import InimDataUpdateCoordinator
from .room_guess import guess_room
from .zone_guess import _fold


def panel_device_info(coordinator: InimDataUpdateCoordinator) -> DeviceInfo:
    """Build the DeviceInfo for the panel hub device, with correct versions.

    Firmware/model come from the cgi ``version`` (parsed correctly: firmware
    from ``primex``, not the API-version field). When the read-only 6004 channel
    has supplied the precise firmware string (e.g. ``"4.07 PX020"``) we prefer it
    for the exact model variant the cgi only templates as ``PXxxx``.
    """
    entry = coordinator.config_entry
    version = coordinator.data.version
    firmware = version.firmware
    model = version.model_name

    local = coordinator.local_config
    if local is not None and local.firmware:
        parts = local.firmware.split(" ", 1)
        if parts[0]:
            firmware = parts[0]
        if len(parts) > 1 and any(c.isdigit() for c in parts[1]):
            model = parts[1].strip()

    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="INIM",
        model=model,
        sw_version=firmware,
        name=entry.title,
    )


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
        suggested_area=room,
        manufacturer="INIM",
        via_device=(DOMAIN, entry.entry_id),
    )


def parse_room_overrides(text: str) -> list[tuple[str, str]]:
    """Parse the room-override option into ordered ``(folded_pattern, room)`` pairs.

    Each non-empty, non-``#`` line is ``pattern = room``; the pattern is matched
    accent- and case-insensitively as a substring of the zone label. Order is
    preserved, so the first matching line wins (list the most specific first).
    """
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        pattern, _, room = line.partition("=")
        folded = _fold(pattern.strip())
        room = room.strip()
        if folded and room:
            pairs.append((folded, room))
    return pairs


def apply_room_overrides(label: str, overrides: list[tuple[str, str]]) -> str | None:
    """Return the overridden room for ``label``, or None when no override matches."""
    folded = _fold(label)
    for pattern, room in overrides:
        if pattern in folded:
            return room
    return None


def resolve_room(coordinator: InimDataUpdateCoordinator, label: str) -> str | None:
    """Resolve a zone's room: user overrides first, then the label-based guess."""
    raw = coordinator.config_entry.options.get(CONF_ROOM_OVERRIDES, DEFAULT_ROOM_OVERRIDES)
    override = apply_room_overrides(label, parse_room_overrides(raw if isinstance(raw, str) else ""))
    if override is not None:
        return override
    return guess_room(label, label_language(coordinator))
