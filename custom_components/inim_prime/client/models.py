"""Typed dataclasses parsed from the panel's stringly-typed JSON. No I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .const import AreaMode, AreaState, ZoneState


def parse_decimal(value: str) -> float:
    return float(value.replace(",", "."))


@dataclass(frozen=True)
class Version:
    version: str
    verhttp: str
    primex: str
    servizio: bool

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> Version:
        return cls(
            version=d["version"],
            verhttp=d["verhttp"],
            primex=d["primex"],
            servizio=bool(d["servizio"]),
        )


@dataclass(frozen=True)
class Zone:
    id: int
    label: str
    terminal: int
    state: ZoneState
    alarm_memory: bool
    excluded: bool

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> Zone:
        return cls(
            id=int(d["id"]),
            label=d["lb"].strip(),
            terminal=int(d["tl"]),
            state=ZoneState(int(d["st"])),
            alarm_memory=d["mm"] == "1",
            excluded=d["by"] == "0",
        )


@dataclass(frozen=True)
class Area:
    id: int
    label: str
    mode: AreaMode
    state: AreaState
    alarm_memory: bool

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> Area:
        return cls(
            id=int(d["id"]),
            label=d["lb"].strip(),
            mode=AreaMode(int(d["am"])),
            state=AreaState(int(d["st"])),
            alarm_memory=d["mm"] == "1",
        )


@dataclass(frozen=True)
class Scenario:
    id: int
    label: str
    active: bool

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> Scenario:
        return cls(id=int(d["id"]), label=d["lb"].strip(), active=d["st"] == "1")


@dataclass(frozen=True)
class Output:
    id: int
    label: str
    terminal: int
    state: int
    type: int

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> Output:
        return cls(
            id=int(d["id"]),
            label=d["lb"].strip(),
            terminal=int(d["tl"]),
            state=int(d["st"]),
            type=int(d["t"]),
        )


# Per-bit fault layout of the ``fau`` bitmap, taken from the INIM PrimeX
# firmware fault structure: byte 0 is bits 0..7, byte 1 is bits 8..15, in this
# documented order. The two leading ``available_*`` bits are reserved/unused and
# are NOT exposed as flags. IMPORTANT: we have only ever observed fau="0" on a
# healthy panel, so this bit order MUST be re-verified against a real fault
# event before being trusted in production.
_FAULT_BIT_ORDER: tuple[str | None, ...] = (
    # byte 0 (bits 0..7)
    None,  # available_1 (reserved)
    None,  # available_2 (reserved)
    "low_battery",
    "fault_mains",  # rete
    "no_phone_line",  # linetel
    "jam_radio",
    "low_battery_wls",
    "disappearance_wls",
    # byte 1 (bits 8..15)
    "fault_gsm",
    "sensor_dirty",
    "zone_fault",  # zona guasto
    "sirens",
    "power_supply",
    "radio_keypads",
    "tamper",  # scomp_sab
    "comm_internet",  # scomp_internet
)

# The real (non-reserved) fault flag keys, in documented order. Used to build a
# fully-False mapping and to drive the per-fault binary_sensors.
FAULT_FLAG_KEYS: tuple[str, ...] = tuple(k for k in _FAULT_BIT_ORDER if k is not None)


def _all_flags_false() -> dict[str, bool]:
    return {key: False for key in FAULT_FLAG_KEYS}


def _truthy(bit: object) -> bool:
    """Coerce a fault-bit value to bool without ever raising.

    Accepts int/float, numeric strings, and textual booleans (on/true/yes);
    anything unrecognized is treated as False (never crash the coordinator poll).
    """
    if isinstance(bit, bool):
        return bit
    if isinstance(bit, (int, float)):
        return bit != 0
    if isinstance(bit, str):
        s = bit.strip().lower()
        if s in ("1", "on", "true", "yes", "y"):
            return True
        if s in ("", "0", "off", "false", "no", "n"):
            return False
        try:
            return float(s) != 0
        except ValueError:
            return False
    return False


def _parse_fault_flags(fau: object) -> dict[str, bool]:
    """Parse the ``fau`` value into a flag-name -> bool mapping, defensively.

    Handles the several shapes we might encounter: "0"/0 (no faults), a numeric
    int/string little-endian bitmap, a nested {"byte 0": {...}, "byte 1": {...}}
    dict, a flat {name: 0/1} dict, or anything unrecognized (-> all False).
    """
    flags = _all_flags_false()

    # No faults.
    if fau == 0 or (isinstance(fau, str) and fau.strip() == "0"):
        return flags

    # Dict form: nested per-byte sub-dicts or a flat {name: 0/1} mapping.
    if isinstance(fau, dict):
        for value in fau.values():
            if isinstance(value, dict):
                # Nested {"byte 0": {name: 0/1}, ...}.
                for name, bit in value.items():
                    if name in flags:
                        flags[name] = _truthy(bit)
        # Flat {name: 0/1} mapping.
        for name, bit in fau.items():
            if name in flags:
                flags[name] = _truthy(bit)
        return flags

    # Numeric / numeric-string little-endian bitmap.
    try:
        bitmap = int(str(fau).strip())
    except (TypeError, ValueError):
        return flags
    for index, key in enumerate(_FAULT_BIT_ORDER):
        if key is not None and bitmap & (1 << index):
            flags[key] = True
    return flags


@dataclass(frozen=True)
class Fault:
    vcc: float
    raw_fau: str
    has_faults: bool
    flags: dict[str, bool] = field(default_factory=_all_flags_false)

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> Fault:
        fau = d["fau"]
        return cls(
            vcc=parse_decimal(d["vcc"]),
            raw_fau=str(fau),
            has_faults=str(fau) != "0",
            flags=_parse_fault_flags(fau),
        )


@dataclass(frozen=True)
class Timer:
    id: int
    label: str
    active: bool

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> Timer:
        return cls(id=int(d["id"]), label=d["lb"].strip(), active=d["st"] == "1")


@dataclass(frozen=True)
class ApiStats:
    api: str
    connections: int
    last_connection: str
    last_ip: str

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> ApiStats:
        return cls(
            api=d["api"],
            connections=int(d["nc"]),
            last_connection=d["lc"],
            last_ip=d["lip"],
        )


@dataclass(frozen=True)
class IpAcl:
    only_enabled: bool
    ips: list[str]

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> IpAcl:
        return cls(
            only_enabled=d["soloIpAbilitati"],
            ips=[x["IP"] for x in d["listaIP"] if x["IP"] != "255.255.255.255"],
        )


@dataclass(frozen=True)
class MacAcl:
    only_enabled: bool
    macs: list[str]

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> MacAcl:
        return cls(
            only_enabled=d["soloMacAbilitati"],
            macs=[x["MAC"] for x in d["listaMAC"] if x["MAC"] != "FF-FF-FF-FF-FF-FF"],
        )


@dataclass(frozen=True)
class OpenZone:
    id: int
    label: str

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> OpenZone:
        return cls(id=int(d["id"]), label=d["lb"].strip())
