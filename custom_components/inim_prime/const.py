"""Constants for the INIM Prime integration."""

from __future__ import annotations

import logging
import re
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "inim_prime"

LOGGER: Final = logging.getLogger(__package__)

# Factory-default INIM area names look like "AREA       006 " (uppercase
# literal "AREA" + whitespace + a number). Used areas have real, custom names
# (e.g. "Garage", "Day Zone") or a manually title-cased "Area 5". The match
# is case-SENSITIVE so only the untouched factory pattern is treated as unused.
_FACTORY_AREA_RE: Final = re.compile(r"AREA\s+\d+")


def is_factory_default_area(label: str) -> bool:
    """Return True iff ``label`` is an INIM factory-default area name.

    Matches the case-sensitive uppercase pattern ``AREA\\s+\\d+`` against the
    stripped label (e.g. "AREA 006".."AREA 010") but not real names such as
    "Garage", "Outdoor", "Day Zone", the manually set title-case "Area 5", or
    an empty string.
    """
    return _FACTORY_AREA_RE.fullmatch(label.strip()) is not None


PLATFORMS: Final[list[Platform]] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.BUTTON,
]

# Realtime / push (local webhook) options.
CONF_WEBHOOK_ENABLED: Final = "webhook_enabled"
CONF_WEBHOOK_ID: Final = "webhook_id"

# Language used to guess zone device classes (icons) from labels. "auto" follows
# the Home Assistant system language; otherwise an explicit ISO-639-1 code.
CONF_LABEL_LANGUAGE: Final = "label_language"
LABEL_LANGUAGE_AUTO: Final = "auto"

# Group each zone under a per-room device, guessing the room from the label.
CONF_GROUP_BY_ROOM: Final = "group_by_room"
DEFAULT_GROUP_BY_ROOM: Final = True

# Require the panel to verify a user code before any arm/disarm. Off by default:
# the LAN password is the sole credential the panel checks on 6004, so enabling
# this makes Home Assistant ask for an INIM user code and have the PANEL validate
# it (op 0x04) before sending the command. The code is never stored. With it on,
# the one-tap scenario buttons are disabled (a button cannot prompt for a code);
# use the area alarm panels, which show a keypad.
CONF_REQUIRE_CODE: Final = "require_code"
DEFAULT_REQUIRE_CODE: Final = False

# Which panel areas get entities, as area ids (strings, the options flow's
# value type). Unset until the owner saves a selection: every area with a real
# (non-factory) name is shown. See areas.py.
CONF_AREAS: Final = "areas"
# The area ids the selection was made from, so an area added on the panel
# afterwards can be told apart from one the owner deselected.
CONF_AREAS_OFFERED: Final = "areas_offered"

# The panel's native protocol (TCP 6004) is the only channel: structure, live
# state and commands. The password is the panel LAN password (default "pass"),
# validated at config time; setup fails without it.
CONF_LOCAL_PASSWORD: Final = "local_6004_password"
DEFAULT_LOCAL_PASSWORD: Final = "pass"
LOCAL_6004_PORT: Final = 6004
# Friendly name for the panel; used as the config-entry title and the device
# name (so entities read e.g. "Alarm Front Door", not the panel IP).
DEFAULT_NAME: Final = "INIM Prime"

# Seconds between live-state reads (a handful of ~10 ms status commands).
NATIVE_POLL_INTERVAL: Final = 2
# Seconds between reads of the slow-changing values (battery temperature and
# internal resistance), within the regular cycle.
SLOW_POLL_INTERVAL: Final = 60
# Seconds between reads of the LTE communicator status: the panel queries the
# modem for it, so it is read rarely.
GSM_POLL_INTERVAL: Final = 300
# Seconds between reads once FAILURES_BEFORE_BACKOFF cycles in a row failed, so
# a panel that stopped answering is not hammered.
NATIVE_BACKOFF_INTERVAL: Final = 30
# Hard ceiling for one read cycle; a cycle that exceeds it is aborted and
# reported as failed, so it can never run away or pile up.
DEFAULT_CYCLE_TIMEOUT: Final = 20
# Consecutive failed cycles after which the reads slow down.
FAILURES_BEFORE_BACKOFF: Final = 3
# Consecutive failed cycles tolerated (serving the last good snapshot) before
# entities are marked unavailable, so one slow answer does not flap every entity.
FAILURES_BEFORE_UNAVAILABLE: Final = 3

# Keys of the cgi-era config entries (version 1), dropped by the migration.
LEGACY_DATA_KEYS: Final = ("apikey", "port", "use_https", "scan_interval")
LEGACY_OPTION_KEYS: Final = (
    "scan_interval",
    "scan_interval_idle",
    "scan_interval_active",
    "native_area_poll",
    "native_commands",
)

# Panel event names carried in the webhook query/body (see design doc §2.3).
EV_ZONE_OPEN: Final = "zone_open"
EV_ZONE_CLOSE: Final = "zone_close"
EV_ARM: Final = "arm"
EV_DISARM: Final = "disarm"
EV_ALARM: Final = "alarm"
EV_TAMPER: Final = "tamper"
EV_FAULT: Final = "fault"
EV_FAULT_RESTORE: Final = "fault_restore"
EV_OUTPUT: Final = "output"

KNOWN_EVENTS: Final[frozenset[str]] = frozenset(
    {
        EV_ZONE_OPEN,
        EV_ZONE_CLOSE,
        EV_ARM,
        EV_DISARM,
        EV_ALARM,
        EV_TAMPER,
        EV_FAULT,
        EV_FAULT_RESTORE,
        EV_OUTPUT,
    }
)

# Maximum accepted webhook body size (bytes) — see design doc §2.5.
MAX_WEBHOOK_BODY: Final = 1024
