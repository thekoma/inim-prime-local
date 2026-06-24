"""Constants and enums for the INIM PrimeX API. No I/O."""
from enum import IntEnum, StrEnum

API_PATH = "/cgi-bin/api.cgi"


class ApiStatus(IntEnum):
    SUCCESS = 0
    ERROR_PARAM = 1
    ERROR_APIKEY = 2
    ERROR_COMMAND = 3
    ERROR_EXECUTION = 4
    ERROR_PROTOCOL = 5
    ERROR_AUTHORIZATION = 6
    NOT_IMPLEMENTED = 7
    CODE_NOT_ALLOWED = 8
    ZONES_NOT_READY = 11


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
    CLEAR_MEMORY = 5


class Command(StrEnum):
    VERSION = "version"
    PING = "ping"
    GET_ZONES = "get_zones_status"
    GET_OUTPUTS = "get_outputs_status"
    GET_PARTITIONS = "get_partitions_status"
    GET_SCENARIOS = "get_scenarios_status"
    GET_FAULTS = "get_faults_status"
    GET_GSM = "get_gsm_status"
    GET_TIMERS_STATUS = "get_timers_status"
    GET_STATUS_API = "get_status_api"
    GET_IP_AUTH = "get_ip_autorizzati"
    GET_MAC_AUTH = "get_mac_autorizzati"
    GET_PARTITIONS_NRZ = "get_partitions_nrz"
    GET_SCENARIOS_NRZ = "get_scenarios_nrz"
    SET_PARTITIONS = "set_partitions_mode"
    SET_SCENARIOS = "set_scenarios_mode"
    SET_OUTPUTS = "set_outputs_mode"
    SET_ZONES = "set_zones_mode"
