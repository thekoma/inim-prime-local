"""Constants and enums for the INIM PrimeX panel state. No I/O."""
from enum import IntEnum


class ZoneState(IntEnum):
    FAULT = 0
    READY = 1
    ALARM = 2
    SHORT_CIRCUIT = 3

    @property
    def is_open(self) -> bool:
        return self is ZoneState.ALARM


class AreaMode(IntEnum):
    TOTAL = 1
    PARTIAL = 2
    SNAPSHOT = 3
    DISARMED = 4

    @property
    def is_armed(self) -> bool:
        return self is not AreaMode.DISARMED


class AreaState(IntEnum):
    ALARM = 0
    READY = 1
    SABOTAGE = 2


class ArmMode(IntEnum):
    TOTAL = 1
    PARTIAL = 2
    SNAPSHOT = 3
    DISARM = 4
