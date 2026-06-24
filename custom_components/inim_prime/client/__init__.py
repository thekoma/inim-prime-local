"""Vendored INIM PrimeX API client — self-contained, no external package needed."""

from .api import (
    InimApiError,
    InimConnectionError,
    InimError,
    InimPrimeClient,
)
from .const import (
    API_PATH,
    ApiStatus,
    AreaMode,
    AreaState,
    ArmMode,
    Command,
    ZoneState,
)
from .models import (
    FAULT_FLAG_KEYS,
    ApiStats,
    Area,
    Fault,
    IpAcl,
    MacAcl,
    OpenZone,
    Output,
    Scenario,
    Timer,
    Version,
    Zone,
    parse_decimal,
)

__all__ = [
    "API_PATH",
    "ApiStatus",
    "ArmMode",
    "AreaMode",
    "AreaState",
    "Command",
    "ZoneState",
    "ApiStats",
    "Area",
    "Fault",
    "FAULT_FLAG_KEYS",
    "IpAcl",
    "MacAcl",
    "OpenZone",
    "Output",
    "parse_decimal",
    "Scenario",
    "Timer",
    "Version",
    "Zone",
    "InimApiError",
    "InimConnectionError",
    "InimError",
    "InimPrimeClient",
]
