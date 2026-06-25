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
# (e.g. "Box", "Appartam.Giorno") or a manually title-cased "Area 5". The match
# is case-SENSITIVE so only the untouched factory pattern is treated as unused.
_FACTORY_AREA_RE: Final = re.compile(r"AREA\s+\d+")


def is_factory_default_area(label: str) -> bool:
    """Return True iff ``label`` is an INIM factory-default area name.

    Matches the case-sensitive uppercase pattern ``AREA\\s+\\d+`` against the
    stripped label (e.g. "AREA 006".."AREA 010") but not real names such as
    "Box", "Esterno", "Appartam.Giorno", the manually set title-case "Area 5",
    or an empty string.
    """
    return _FACTORY_AREA_RE.fullmatch(label.strip()) is not None

PLATFORMS: Final[list[Platform]] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.BUTTON,
]

CONF_APIKEY: Final = "apikey"
CONF_USE_HTTPS: Final = "use_https"

# Realtime / push (local webhook) options.
CONF_WEBHOOK_ENABLED: Final = "webhook_enabled"
CONF_WEBHOOK_ID: Final = "webhook_id"
CONF_SCAN_INTERVAL_IDLE: Final = "scan_interval_idle"
CONF_SCAN_INTERVAL_ACTIVE: Final = "scan_interval_active"

# Language used to guess zone device classes (icons) from labels. "auto" follows
# the Home Assistant system language; otherwise an explicit ISO-639-1 code.
CONF_LABEL_LANGUAGE: Final = "label_language"
LABEL_LANGUAGE_AUTO: Final = "auto"

DEFAULT_PORT: Final = 8080
DEFAULT_SCAN_INTERVAL: Final = 15
DEFAULT_USE_HTTPS: Final = False

# Adaptive two-tier polling defaults (seconds).
DEFAULT_SCAN_INTERVAL_IDLE: Final = 30
DEFAULT_SCAN_INTERVAL_ACTIVE: Final = 1
# How long (seconds) to stay in the fast/active tier after an event.
DEFAULT_ACTIVE_WINDOW: Final = 20

# Overload / timeout hardening (seconds, except the failure count).
# Per-cgi-request ceiling used by the polling client (a full cycle issues
# several sequential reads, so this is well under DEFAULT_CYCLE_TIMEOUT).
DEFAULT_REQUEST_TIMEOUT: Final = 5
# Hard ceiling for an entire update cycle; a cycle that exceeds this is aborted
# and reported as UpdateFailed so it can never run away or pile up.
DEFAULT_CYCLE_TIMEOUT: Final = 8
# Consecutive failed cycles after which fast polling is suspended and the
# coordinator backs off to the idle tier.
FAILURES_BEFORE_BACKOFF: Final = 3

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
