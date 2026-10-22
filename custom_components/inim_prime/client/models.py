"""Typed dataclasses for the panel state the integration exposes. No I/O."""

from __future__ import annotations

from dataclasses import dataclass, field

from .const import AreaMode, AreaState, ZoneState


@dataclass(frozen=True)
class Version:
    # The panel's version string as read natively, e.g. "5.00 PX020".
    raw: str
    # The panel is in service (its SERV jumper is set).
    service: bool

    @property
    def firmware(self) -> str:
        """Panel firmware version, e.g. ``5.00`` (the first token of ``raw``)."""
        return self.raw.split(" ", 1)[0]

    @property
    def model_name(self) -> str:
        """Panel model, e.g. ``PX020``.

        Only a model carrying digits is a precise variant; otherwise the
        friendly family name ``PrimeX`` is shown.
        """
        parts = self.raw.split(" ", 1)
        model = parts[1].strip() if len(parts) > 1 else ""
        return model if any(c.isdigit() for c in model) else "PrimeX"


@dataclass(frozen=True)
class Zone:
    id: int
    label: str
    terminal: int
    state: ZoneState
    alarm_memory: bool
    excluded: bool


@dataclass(frozen=True)
class Area:
    id: int
    label: str
    mode: AreaMode
    state: AreaState
    alarm_memory: bool


@dataclass(frozen=True)
class Scenario:
    id: int
    label: str
    active: bool


@dataclass(frozen=True)
class Output:
    id: int  # the output's terminal
    label: str
    # None until the output's state has been read.
    state: int | None


# The panel fault flags the integration exposes, in the panel's own order (see
# local6004.decode_system_status for the bits they are read from).
FAULT_FLAG_KEYS: tuple[str, ...] = (
    "low_battery",
    "fault_mains",
    "no_phone_line",
    "jam_radio",
    "low_battery_wls",
    "disappearance_wls",
    "fault_gsm",
    "sensor_dirty",
    "zone_fault",
    "sirens",
    "power_supply",
    "radio_keypads",
    "tamper",
    "comm_internet",
)


def _all_flags_false() -> dict[str, bool]:
    return dict.fromkeys(FAULT_FLAG_KEYS, False)


@dataclass(frozen=True)
class Fault:
    vcc: float  # switching supply output, volts
    has_faults: bool
    flags: dict[str, bool] = field(default_factory=_all_flags_false)
    # The fault word and panel flags as read (hex), for diagnostics.
    raw: str = ""
