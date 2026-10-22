"""Client for the INIM PrimeX local protocol (TCP 6004).

The integration's only channel to the panel. What it reads and sends:

* **static structure** — which partitions, zones, arming scenarios and outputs
  exist, their labels and the zones' programmed kind, read once at setup (see
  :class:`Local6004Structure`), with the **zone -> area** mapping;
* **live status** — partitions (op 6), terminals (op 7: zones and outputs), the
  panel-wide faults, tampers and voltages (op 0x0D), polled, and the battery
  power values (op 0x31) and the LTE communicator status (op 0x5E), polled
  less often;
* **multi-active scenes** — each arming scenario's per-partition target mode
  (``away``/``stay``/``disarm``); a scenario is "active" when every partition it
  targets currently matches that mode;
* five **write commands** (see "Write commands" below) and the event log.

Protocol (reverse-engineered + verified on a live capture; AES key = the panel
LAN password): each frame is ``50 50 | CRC16-ARC(LE over [+4..end]) | flag(LE) |
LEN(LE total) | 00 00 | AES-128-CBC ciphertext``. A connection opens with a
context op-code; reads use op 0x11 (start) / 0x10 (continue) with
``[addr:4 LE][0000:4][len:4 LE][len:4 LE][00 00][op][chk=sum(prev19)&0xff]``.

⚠️ Opcode allow-lists: this module only ever emits the two memory-read
opcodes, the read-only *status* commands (op 6 partition statuses, op 7
terminal statuses, op 0x0D system status, op 0x31 power values, op 0x5E GSM
status), the read-only *code check* query (op 0x04, validates a user code) and
the five *write* commands below. It never builds a memory-write/program frame
(the same framing with another opcode — on a production panel a stray write
could brick it). ``_read_cmd``, ``_status_cmd``, ``check_code_cmd`` and
``_write_cmd`` each check their opcode against their own literal allow-list and
raise :class:`OpcodeNotAllowed` otherwise (:class:`ReadOnlyViolation` for the
read-only ones; an explicit check, not an ``assert``, so ``python -O`` cannot
drop it).

Write commands (same source, verified live on PrimeX 4.07 and 5.00; op 0x39 from
libdriver and the PrimeX firmware): request
``[op:4 LE][pin:6][data]``, where the PIN is one digit per byte padded with
0xFF (``74 00 00 00 00 00`` = no PIN):

* op 3 set arming status: 30 bytes, one per partition, holding the target
  :class:`AreaMode` (0 = leave that partition alone);
* op 8 set output: ``[terminal:2 LE][1 on | 0 off:2 LE]``;
* op 9 set zone bypass: ``[terminal:2 LE][0 bypass | 2 unbypass:2 LE]``,
  plus 1 in the value for the terminal's second zone (half B);
* op 16 reset partitions (alarm memory): ``[partition bitmask:4 LE]``;
* op 0x39 clear the call queue (libdriver "stop_actions", the keypad's "Canc.
  telefonate"): ``[partition bitmask:4 LE][2]``; the other actions of this
  opcode (stop alarms, reset alarm memory) are never sent.

Pitscheider's library does not decode the command response ("complete
handling of the command response envelope" is listed as not implemented), so
there is no documented success/failure code. What was observed on a PrimeX
4.07 for the status commands: the 18-byte response header starts with the
bitwise NOT of the opcode (uint32 LE), then a constant
``01 00 ff ff ff 03 00 00 ff ff ff ff ff ff``. A write response is therefore
only checked for that opcode echo, and its header is logged at debug level.

Live partition status (op 6) follows the command layout documented by
Pitscheider's inim-prime-native (https://github.com/Pitscheider/inim-prime-native,
GPL-3.0): request ``[op:4 LE][pin:6]`` (``74 00..`` = no PIN), response
``[header:18][3 bytes x 30 partitions]``; each record is
``[state][AreaMode][flags]``, state 1 = alarm and 2 = sabotage, flags ``0x10``
configured, ``0x01`` alarm memory and ``0x02`` sabotage memory. It answers in
~10 ms.

Live terminal status (op 7, same source) takes ``[start:2 LE][end:2 LE]``
(end exclusive, at most 20 terminals) and answers ``[header:18][10 bytes x 20]``.
A record is ``[type][00][zone A: flags, 00, ZoneState, 00][zone B: ...]`` with
type 0 = single zone, 3 = double zone, 1 = output, 4 = disabled; flags
``0x10`` = excluded (bypassed), ``0x01`` = alarm memory. Zone ``n`` (< 1005) is
half A of terminal ``n``; zone ``n + 1005`` is half B of terminal ``n`` (verified
on a live PrimeX 4.07).

The static structure uses the label tables and the zone-settings layout also
documented by Pitscheider's library: 16-byte ASCII label records per
partition, zone (indexed by zone id), arming scenario and output (indexed by
``terminal - 1005``), and 11-byte zone settings whose first 4 bytes are the
uint32 LE partition bitmask.

The EEPROM config offsets below are the **40x** layout (PrimeX firmware 4.x),
which is a compile-time-constant memory map in the official client. They are
NOT valid for other firmware families, so callers must gate on the firmware
major version (see :attr:`Local6004Config.layout_ok`).

Firmware 5.00 (PrimeX/Prime, verified read-only on a live panel in October
2026) keeps the framing, the crypto, the version read and the live status and
write commands, but answers with the ``50 54`` preamble and moves most config
tables. Its map is :data:`_LAYOUT_50X`: area and zone label tables are
unchanged, the scenario-mode, scenario-label, output-label, zone-settings and
zone-kind tables moved, and the half-B zone id and the output terminals shifted
from ``+1005`` to ``+1505`` (zone ids 1005+ are the new CoreSync video zones).
The event log keeps its record layout but numbers its event codes differently:
codes are named with the class table the panel reports (decode_event_classes).
A firmware whose major digit is not in :data:`_LAYOUTS` is read for its version
only (``layout_ok=False``) and is not supported.
"""

from __future__ import annotations

import asyncio
import bisect
import datetime as dt
import logging
import re
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import AreaMode, ZoneState
from .event_types import EVENT_OBJECT_KINDS, EVENT_TYPE_NAMES
from .models import FAULT_FLAG_KEYS

_LOGGER = logging.getLogger(__name__)

PORT = 6004
_PREAMBLE = b"\x50\x50"
# Response preambles seen on live panels: ``50 50`` on firmware 4.x, ``50 54``
# on firmware 5.00 (same framing, crypto and commands otherwise).
_RESPONSE_PREAMBLES = frozenset({b"\x50\x50", b"\x50\x54"})
_READ_START = 0x11
_READ_CONT = 0x10
# The only memory opcodes ever sent (literal, independent of the names above).
_READ_OPS = frozenset({0x10, 0x11})
_CHUNK = 1024

# Connection-open context op-codes.
_OPEN_CFG = b"\x17\x00\x00\x00\x00"  # 0x140xxxxx config region
_OPEN_VER = b"\x0d\x00\x00\x00\x00"  # version region
_OPEN_LOG = b"\x1f\x00\x00\x00\x00"  # low-address event-log region

# Read-only status command: live partition statuses.
_OP_PARTITION_STATUS = 6
_NO_PIN = b"\x74\x00\x00\x00\x00\x00"
_STATUS_HEADER = 18
_PARTITION_REC = 3
_PARTITION_COUNT = 30
_PARTITION_CONFIGURED = 0x10
# Partition record byte 0 (Prime/STUDIO Comm::AreeRealTime).
_AREA_STATE_ALARM = 1
_AREA_STATE_SABOTAGE = 2
_CLOSE_TIMEOUT = 1.0

# Read-only status command: live terminal (zone) statuses.
_OP_TERMINAL_STATUS = 7
_TERMINAL_REC = 10
_TERMINAL_CHUNK = 20
_TERMINAL_SINGLE = 0
_TERMINAL_OUTPUT = 1
_TERMINAL_DOUBLE = 3
_ZONE_EXCLUDED = 0x10
_ZONE_MEMORY = 0x01
# Zone id of the second half (B) of terminal 0 on a double-zone terminal.
SECOND_HALF_ZONE_OFFSET = 1005
# Every terminal id: zone terminals 0..1004, then the panel outputs 1005..1009.
_TERMINAL_COUNT = 1010
_OUTPUT_TERMINALS = range(SECOND_HALF_ZONE_OFFSET, _TERMINAL_COUNT)
# Per-command ceiling for the live status reads. Cold reads after the channel
# sat idle were measured at 3.4-4 s, so 3 s timed out on a healthy panel.
STATUS_TIMEOUT = 5.0
# Ceiling for each step of the one-off structure read at setup (its own budget,
# outside the config read's): a step can take ~50 round trips, each of which
# can be slow on a cold channel.
STRUCTURE_TIMEOUT = 30.0
# Failures of a best-effort structure step: that object kind is then unknown.
_STEP_ERRORS = (TimeoutError, OSError, ValueError, asyncio.IncompleteReadError)

# Read-only system status (libdriver "read_status"): faults, panel flags and the
# supply voltages; Prime/STUDIO polls it about once a second while monitoring.
_OP_SYSTEM_STATUS = 0x0D
# Read-only power values (libdriver "get_power_values"): battery temperature and
# internal resistance, output voltages and currents; Prime/STUDIO's Power page.
_OP_POWER_VALUES = 0x31
# Read-only LTE communicator status (libdriver "read_gsm_4g_data"): signal,
# networks, faults, operator code. Prime/STUDIO's GSM page. The panel asks the
# modem for it, so it is read rarely.
_OP_GSM_STATUS = 0x5E
# The only status (read-only command) opcodes ever sent. Literal, so a renamed
# or mistyped constant above cannot widen it.
_STATUS_OPS = frozenset({6, 7, 0x0D, 0x31, 0x5E})
# What precedes a status command's own data: the no-PIN marker for the live
# partition and terminal status, a single 0x00 for the system status and the
# power values, 32 zero bytes for the GSM status (as Prime/STUDIO sends it).
_STATUS_PREFIX = {
    6: _NO_PIN,
    7: _NO_PIN,
    0x0D: b"\x00",
    0x31: b"\x00",
    0x5E: bytes(32),
}

# Read-only code check (op 0x04, "controllo_codice"/get_code_type): validates a
# user code and returns its type/permissions without changing anything. It is
# read-only but carries the code in its data section, so it has its own builder
# and one-element allow-list, like the status and write builders.
_OP_CHECK_CODE = 4
_QUERY_OPS = frozenset({_OP_CHECK_CODE})

# Write commands (Pitscheider's CommandOperation, plus libdriver's
# "stop_actions"). Sent only by the async_set_*/async_reset_*/async_clear_*
# methods.
_OP_SET_ARMING = 3
_OP_SET_OUTPUT = 8
_OP_SET_ZONE_BYPASS = 9
_OP_RESET_PARTITIONS = 16
# "stop_actions": [partition bitmask u32 LE][action]. The action picks stop
# alarms (1), clear the call queue (2) or reset alarm memory (3) (PrimeX 4.07
# firmware command handler); only the call-queue action is ever sent, and it
# clears every channel's queue whatever the mask (the keypad's "Canc.
# telefonate").
_OP_STOP_ACTIONS = 0x39
_ACTION_CLEAR_CALLS = 2
# The only state-changing opcodes ever sent (literal, see _STATUS_OPS).
_WRITE_OPS = frozenset({3, 8, 9, 16, 0x39})
_PIN_LEN = 6
_PIN_PAD = 0xFF
_OUTPUT_ON = 1
_OUTPUT_OFF = 0
# Pitscheider: bypass -> 0, un-bypass -> 2. Bit 0 picks the terminal's second
# zone (half B): Prime/STUDIO on 5.00 bypasses zone 16 + half_offset by sending
# terminal 16 with 1, and un-bypasses it with 3 (captured live).
_BYPASS_ON = 0
_BYPASS_OFF = 2
_BYPASS_HALF_B = 1
# Bytes of the response header that echo the opcode (as its bitwise NOT).
_ECHO_LEN = 4
# Per-command ceiling for a write: a liveness read (itself capped at
# STATUS_TIMEOUT), an optional live zone pre-check and the write, each of
# which can take ~4 s on a cold channel. A timeout after the write was sent leaves its outcome unknown, so
# this is generous.
COMMAND_TIMEOUT = 15.0
# I/O failures of one exchange on the persistent connection.
_IO_ERRORS = (TimeoutError, OSError, ValueError, asyncio.IncompleteReadError)

# Event log (40x): ring of 14-byte records at 0xA1D0.
_LOG_ADDR = 0xA1D0
_LOG_LEN = 56000
_LOG_REC = 14
_EPOCH2000 = dt.datetime(2000, 1, 1)

# The event-log context op (0x1F, libdriver "get_logger_rules") answers, after
# its 4-byte echo, (u32 address, u32 length) pairs; the first is the panel's
# event-class table: 5-byte entries [u16 LE first code][b2][kind][b4], sorted,
# ending with a sentinel (Prime/STUDIO Comm::LeggiLogger). Live on 5.00: the
# table at 0x1A037C3C, 735 bytes, 145 classes.
_LOG_CLASS_REC = 5
_LOG_CLASS_MAX_LEN = 5 * 512  # a sanity bound on the table length

# 40x (PrimeX 4.x) EEPROM offsets (base 0x14000000). Firmware-version-specific.
_VERSION_ADDR = 0x1A002400
_SCENARIO_MODI_ADDR = 0x1407DFA8
_SCENARIO_MODI_REC = 19
_SCENARIO_COUNT = 50
_ZONE_CFG_ADDR = 0x14073414
_ZONE_CFG_REC = 11
_ZONE_MASK_LEN = 4
# Terminal settings (getValue["spring_exf_prg_terminals"]): one 12-byte record
# per zone terminal, half_offset records, no CRC trailer.
_TERMINAL_CFG_ADDR = 0x140704F8
_TERMINAL_CFG_REC = 12
# Detector type per terminal (getValue["spring_exf_pulse_settings"]): one 28-byte
# record per terminal, outputs included, no CRC trailer; the type is byte 10.
_PULSE_CFG_ADDR = 0x14100A10
_PULSE_CFG_REC = 28
_PULSE_SENSOR = 10
# A third option byte per zone (getValue["spring_exf_opt_zone_1"]).
_ZONE_OPT2_ADDR = 0x1406F8A0
_LABEL_REC = 16
_AREA_LABELS_ADDR = 0x14030D20
_ZONE_LABELS_ADDR = 0x14030F00
_SCENARIO_LABELS_ADDR = 0x1403D8E0
_OUTPUT_LABELS_ADDR = 0x1403DEB0


@dataclass(frozen=True)
class _Layout:
    """Firmware-family memory map for the config tables (base 0x14000000).

    The live status/command opcodes and the version read are identical across
    families; only the EEPROM offsets, the half-B zone id offset and the
    output-terminal range differ. ``zone_cfg``, the zone-kind tables
    (``terminal_cfg``, ``pulse_cfg``, ``zone_opt2``) and ``log_addr`` are ``None``
    when that table's offset is not mapped for the family yet: the zone->area
    map, the zone kinds and the event log are then unavailable, while the rest of the native channel
    (live area/zone status, scenes, labels) still works. ``native_writes_ok`` is
    False for a family whose write-command responses have not been verified on a
    live panel; the integration then refuses commands on that family.
    """

    name: str
    scenario_modi: int
    area_labels: int
    zone_labels: int
    scenario_labels: int
    output_labels: int
    half_offset: int
    terminal_count: int
    zone_cfg: int | None
    terminal_cfg: int | None
    pulse_cfg: int | None
    zone_opt2: int | None
    log_addr: int | None
    native_writes_ok: bool

    @property
    def output_terminals(self) -> range:
        return range(self.half_offset, self.terminal_count)


_LAYOUT_40X = _Layout(
    name="40x",
    scenario_modi=_SCENARIO_MODI_ADDR,
    area_labels=_AREA_LABELS_ADDR,
    zone_labels=_ZONE_LABELS_ADDR,
    scenario_labels=_SCENARIO_LABELS_ADDR,
    output_labels=_OUTPUT_LABELS_ADDR,
    half_offset=1005,
    terminal_count=1010,
    zone_cfg=_ZONE_CFG_ADDR,
    terminal_cfg=_TERMINAL_CFG_ADDR,
    pulse_cfg=_PULSE_CFG_ADDR,
    zone_opt2=_ZONE_OPT2_ADDR,
    log_addr=_LOG_ADDR,
    native_writes_ok=True,
)
# 50x (PrimeX/Prime firmware 5.00): verified read-only on a live panel in Oct
# 2026. Area and zone label tables are unchanged; the scenario-mode,
# scenario-label, output-label and zone-settings (zone->area) tables moved, and
# the half-B zone id and the output terminals shifted by +500. The offsets are
# recovered by symbol name from the Prime/STUDIO Centrale5.dll's own field table
# (class maps_500::getValue): zone_cfg = getValue["spring_exf_prg_zones"] =
# 0x1408B920, scenario_modi = getValue["spring_exf_prg_scenari"] = 0x14099368.
# See tools/offsets/ (the extractor and its committed output) and
# tests/test_offset_provenance.py, which pins each offset to that DLL-attested
# value. The zone_cfg record is still 11 bytes; only the zone count grew with the
# terminal count (0x1408B920 live-verified on 5.00: 20/20 zones, correct area
# membership). terminal_cfg = getValue["spring_exf_prg_terminals"] = 0x14087294,
# pulse_cfg = getValue["spring_exf_pulse_settings"] = 0x1413C068 and zone_opt2 =
# getValue["spring_exf_opt_zone_1"] = 0x14086060 (all live-verified on 5.00). The
# event log is at spring_eep_logger_ev = 0x104A8, with 4.x's 14-byte records
# (live-verified on 5.00: 1264 records with valid timestamps); its event codes
# are numbered differently, so they are decoded with the class table the panel
# reports (see decode_event_classes).
_LAYOUT_50X = _Layout(
    name="50x",
    scenario_modi=0x14099368,
    area_labels=_AREA_LABELS_ADDR,
    zone_labels=_ZONE_LABELS_ADDR,
    scenario_labels=0x14041760,
    output_labels=0x14041D30,
    half_offset=1505,
    terminal_count=1510,
    zone_cfg=0x1408B920,
    terminal_cfg=0x14087294,
    pulse_cfg=0x1413C068,
    zone_opt2=0x14086060,
    log_addr=0x104A8,
    # Native writes verified live on 5.00 (2026-10-19): single arm/disarm ops
    # answer in ~0.03 s with the same echo as 4.07 and do not lock the panel.
    native_writes_ok=True,
)
# Panel firmware major digit -> memory map. A firmware not listed is read for its
# version only (layout_ok False) and the integration does not support it.
_LAYOUTS: dict[str, _Layout] = {"4": _LAYOUT_40X, "5": _LAYOUT_50X}

# Factory-default scenario names carry their 1-based index: "Scenario 10" or
# "SCENARIO   031". Unused scenarios keep them and are not listed.
_FACTORY_SCENARIO_RE = re.compile(r"(?:Scenario|SCENARIO)\s+(\d+)")

# Scenario MODO nibble bits -> live AreaMode they require.
_MODE_BIT = {0x01: "away", 0x02: "stay", 0x04: "disarm"}
# Only the first 6 MODO bytes of a scenario record are decoded (one nibble per
# partition), so scene definitions cover partitions 0-11 only. A scenario on a
# panel with more partitions may target ones this decode does not see.
_SCENE_MODO_BYTES = 6
SCENE_DECODED_PARTITIONS = 2 * _SCENE_MODO_BYTES
_MODE_TO_AREAMODE: dict[str, AreaMode] = {
    "away": AreaMode.TOTAL,
    "stay": AreaMode.PARTIAL,
    "disarm": AreaMode.DISARMED,
}


# --------------------------------------------------------------------- crypto
def crc16_arc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc & 0xFFFF


def make_key_iv(password: str) -> tuple[bytes, bytes]:
    key = password.encode("ascii", "ignore").ljust(16, b"\x00")[:16]
    iv = bytes(key[i] ^ i for i in range(16))
    return key, iv


def _pad(pt: bytes) -> bytes:
    n = 16 - (len(pt) % 16)
    return pt + bytes([n]) * n


def _unpad(pt: bytes) -> bytes:
    return pt[: -pt[-1]] if pt and 1 <= pt[-1] <= 16 else pt


def _aes(key: bytes, iv: bytes, data: bytes, *, decrypt: bool) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    op = cipher.decryptor() if decrypt else cipher.encryptor()
    return op.update(data) + op.finalize()


def _build_frame(app: bytes, key: bytes, iv: bytes, *, first: bool) -> bytes:
    ct = _aes(key, iv, _pad(app), decrypt=False)
    flag = b"\x01\x00" if first else b"\x00\x00"
    ln = (len(ct) + 10).to_bytes(2, "little")
    frame = bytearray(_PREAMBLE + b"\x00\x00" + flag + ln + b"\x00\x00" + ct)
    frame[2:4] = crc16_arc(bytes(frame[4:])).to_bytes(2, "little")
    return bytes(frame)


class OpcodeNotAllowed(RuntimeError):
    """A frame with an opcode outside its allow-list was about to be built.

    Always a programming error: nothing was sent.
    """


class ReadOnlyViolation(OpcodeNotAllowed):
    """A frame other than a read or a read-only status command was about to be built."""


def _read_cmd(addr: int, length: int, *, cont: bool) -> bytes:
    op = _READ_CONT if cont else _READ_START
    if op not in _READ_OPS:
        raise ReadOnlyViolation(f"opcode {op:#x}")
    body = bytearray()
    body += addr.to_bytes(4, "little")
    body += b"\x00\x00\x00\x00"
    body += length.to_bytes(4, "little")
    body += length.to_bytes(4, "little")
    body += b"\x00\x00"
    body += bytes([op])
    body += bytes([sum(body) & 0xFF])
    return bytes(body)


def _status_cmd(op: int, data: bytes = b"") -> bytes:
    """Build a read-only status command body (no PIN)."""
    if op not in _STATUS_OPS:
        raise ReadOnlyViolation(f"status opcode {op}")
    return op.to_bytes(4, "little") + _STATUS_PREFIX[op] + data


def encode_pin(pin: str | None) -> bytes:
    """Encode a user PIN as the command's 6-byte PIN field.

    One digit per byte, padded with 0xFF; None is the "no PIN" marker the
    read-only status commands also send.
    """
    if pin is None:
        return _NO_PIN
    if not (pin.isascii() and pin.isdigit() and 1 <= len(pin) <= _PIN_LEN):
        raise ValueError("PIN must be 1-6 ASCII digits")
    return bytes(int(d) for d in pin).ljust(_PIN_LEN, bytes([_PIN_PAD]))


def _write_cmd(op: int, data: bytes, pin: str | None = None) -> bytes:
    """Build a write command body: ``[op:4 LE][pin:6][data]``."""
    if op not in _WRITE_OPS:
        raise OpcodeNotAllowed(f"write opcode {op}")
    return op.to_bytes(4, "little") + encode_pin(pin) + data


def check_code_cmd(code: str) -> bytes:
    """Build the read-only code-check query (op 0x04): ``[op:4 LE][no-PIN:6][code:6]``.

    op 0x04 ("controllo_codice"/get_code_type) validates a user code and returns
    its type. The code to check goes in the 6-byte DATA section (one digit per
    byte, padded 0xFF — the same encoding as the PIN field), while the PIN field
    carries the no-PIN marker: the command handlers ignore the PIN field over
    LAN, so the code to check is passed as data (verified live on 4.07/5.00).
    Read-only, gated by its own allow-list like the status and write builders;
    a code that is not 1-6 digits raises ``ValueError``.
    """
    if _OP_CHECK_CODE not in _QUERY_OPS:
        raise ReadOnlyViolation(f"query opcode {_OP_CHECK_CODE}")
    if code is None:
        raise ValueError("a code is required")
    return _OP_CHECK_CODE.to_bytes(4, "little") + _NO_PIN + encode_pin(code)


def _check_area_id(area_id: int) -> None:
    if not 0 <= area_id < _PARTITION_COUNT:
        raise ValueError(f"partition id {area_id} out of range")


def arming_data(modes: Mapping[int, AreaMode]) -> bytes:
    """Encode op 3 data: the target mode per partition, 0 = leave it alone."""
    if not modes:
        raise ValueError("no partitions to set")
    data = bytearray(_PARTITION_COUNT)
    for area_id, mode in modes.items():
        _check_area_id(area_id)
        data[area_id] = AreaMode(mode)
    return bytes(data)


def reset_data(area_ids: Iterable[int]) -> bytes:
    """Encode op 16 data: the uint32 LE bitmask of partitions to reset."""
    mask = 0
    for area_id in area_ids:
        _check_area_id(area_id)
        mask |= 1 << area_id
    if not mask:
        raise ValueError("no partitions to reset")
    return mask.to_bytes(4, "little")


def clear_calls_data(area_ids: Iterable[int]) -> bytes:
    """Encode op 0x39 data for the call-queue clear: ``[bitmask:4 LE][2]``.

    The panel clears every channel's queue whatever the mask; it only checks
    the mask against the partitions it may act on, so the configured areas are
    sent. The action byte is a literal: stopping alarms or resetting alarm
    memory through this opcode is never possible.
    """
    return reset_data(area_ids) + bytes([_ACTION_CLEAR_CALLS])


def zone_bypass_data(
    zone_id: int, excluded: bool, half_offset: int = SECOND_HALF_ZONE_OFFSET
) -> bytes:
    """Encode op 9 data: ``[terminal:2 LE][0 bypass | 2 unbypass, +1 half B:2 LE]``.

    The panel addresses the zone by its terminal, and bit 0 of the value picks
    the terminal's second zone (``terminal + half_offset``). Sent as the zone id,
    a second-half zone is answered with an error status and left as it was.
    """
    if not 0 <= zone_id < 2 * half_offset:
        raise ValueError(f"zone id {zone_id} out of range")
    terminal, half = zone_terminal(zone_id, half_offset)
    value = (_BYPASS_ON if excluded else _BYPASS_OFF) | (_BYPASS_HALF_B if half else 0)
    return terminal.to_bytes(2, "little") + value.to_bytes(2, "little")


def output_data(
    terminal: int, on: bool, output_terminals: range = _OUTPUT_TERMINALS
) -> bytes:
    """Encode op 8 data: ``[terminal:2 LE][1 on | 0 off:2 LE]``."""
    if terminal not in output_terminals:
        raise ValueError(f"output terminal {terminal} out of range")
    value = _OUTPUT_ON if on else _OUTPUT_OFF
    return terminal.to_bytes(2, "little") + value.to_bytes(2, "little")


def check_command_response(op: int, resp: bytes) -> None:
    """Raise :class:`NativeCommandRejected` unless ``resp`` echoes ``op``.

    The echo (bitwise NOT of the opcode) is the only part of the response
    header whose meaning is known; see the module docstring.
    """
    echo = (~op & 0xFFFFFFFF).to_bytes(_ECHO_LEN, "little")
    if resp[:_ECHO_LEN] != echo:
        raise NativeCommandRejected(
            op, f"unexpected response header {resp[:_STATUS_HEADER].hex(' ') or 'empty'}"
        )


def zone_terminal(zone_id: int, half_offset: int = SECOND_HALF_ZONE_OFFSET) -> tuple[int, int]:
    """Return ``(terminal, half)`` holding a zone (half 0 = A, 1 = B)."""
    if zone_id >= half_offset:
        return zone_id - half_offset, 1
    return zone_id, 0


def terminal_chunks(terminals: set[int]) -> list[tuple[int, int]]:
    """Group terminal ids into ``[start, end)`` requests of at most 20 terminals."""
    chunks: list[tuple[int, int]] = []
    for terminal in sorted(terminals):
        if chunks and terminal < chunks[-1][0] + _TERMINAL_CHUNK:
            chunks[-1] = (chunks[-1][0], terminal + 1)
        else:
            chunks.append((terminal, terminal + 1))
    return chunks


def decode_label(rec: bytes) -> str:
    """Decode a 16-byte label record (ASCII, space/NUL padded; 0xFF = erased)."""
    return rec.split(b"\x00", 1)[0].replace(b"\xff", b"").decode("latin-1").strip()


def _label_at(blob: bytes, index: int) -> str:
    return decode_label(blob[index * _LABEL_REC : (index + 1) * _LABEL_REC])


def is_factory_default_scenario(scenario_id: int, label: str) -> bool:
    """Return True iff ``label`` is the untouched name of scenario ``scenario_id``.

    Only a default that matches the scenario's own index counts, so a user
    naming scenario 3 "Scenario 7" keeps it. An empty label also counts.
    """
    match = _FACTORY_SCENARIO_RE.fullmatch(label)
    return not label or (match is not None and int(match.group(1)) == scenario_id + 1)


def decode_terminal_types(resp: bytes, start: int, end: int) -> dict[int, int]:
    """Decode a terminal-status response for ``[start, end)`` into ``{terminal: type}``."""
    count = end - start
    if len(resp) < _STATUS_HEADER + count * _TERMINAL_REC:
        raise ValueError(f"short terminal-status response ({len(resp)} bytes)")
    return {start + idx: resp[_STATUS_HEADER + idx * _TERMINAL_REC] for idx in range(count)}


def decode_configured_partitions(resp: bytes) -> list[int]:
    """Return the ids of partitions with the configured bit set."""
    if len(resp) < _STATUS_HEADER + _PARTITION_REC * _PARTITION_COUNT:
        raise ValueError(f"short partition-status response ({len(resp)} bytes)")
    return [
        area_id
        for area_id in range(_PARTITION_COUNT)
        if resp[_STATUS_HEADER + area_id * _PARTITION_REC + 2] & _PARTITION_CONFIGURED
    ]


def zone_candidates(
    terminal_types: dict[int, int], half_offset: int = SECOND_HALF_ZONE_OFFSET
) -> list[int]:
    """Return the zone ids the terminal types allow, in terminal order.

    A single-zone terminal holds zone ``n`` (half A); a double-zone terminal
    also holds ``n + 1005`` (half B). Outputs, disabled and unknown terminals
    hold no zone. The order is terminal, then half.
    """
    zones: list[int] = []
    for terminal in sorted(t for t in terminal_types if t < half_offset):
        halves = {_TERMINAL_SINGLE: 1, _TERMINAL_DOUBLE: 2}.get(terminal_types[terminal], 0)
        zones.extend(terminal + half * half_offset for half in range(halves))
    return zones


def _half_spans(
    zone_ids: list[int], half_offset: int = SECOND_HALF_ZONE_OFFSET
) -> list[tuple[int, int]]:
    """Cover ``zone_ids`` with one ``[first, last]`` span per zone half.

    Labels and settings are indexed by zone id, so two contiguous reads fetch
    everything without reading the whole 2010-zone tables.
    """
    spans: list[tuple[int, int]] = []
    for half in (
        [z for z in zone_ids if z < half_offset],
        [z for z in zone_ids if z >= half_offset],
    ):
        if half:
            spans.append((min(half), max(half)))
    return spans


@dataclass(frozen=True)
class NativeZoneStatus:
    """Live state of one zone, as reported by the terminal status command."""

    state: ZoneState
    excluded: bool
    alarm_memory: bool


def _decode_zone_half(half: bytes) -> NativeZoneStatus | None:
    try:
        state = ZoneState(half[2])
    except ValueError:
        return None
    return NativeZoneStatus(
        state=state,
        excluded=bool(half[0] & _ZONE_EXCLUDED),
        alarm_memory=bool(half[0] & _ZONE_MEMORY),
    )


def decode_terminal_statuses(
    resp: bytes, start: int, end: int, half_offset: int = SECOND_HALF_ZONE_OFFSET
) -> dict[int, NativeZoneStatus]:
    """Decode a terminal-status response for ``[start, end)`` into ``{zone_id: status}``.

    Only single- and double-zone terminals yield zones; outputs, disabled and
    unknown terminals are skipped. A short response raises ``ValueError``.
    """
    count = end - start
    if len(resp) < _STATUS_HEADER + count * _TERMINAL_REC:
        raise ValueError(f"short terminal-status response ({len(resp)} bytes)")
    out: dict[int, NativeZoneStatus] = {}
    for idx in range(count):
        rec = resp[_STATUS_HEADER + idx * _TERMINAL_REC : _STATUS_HEADER + (idx + 1) * _TERMINAL_REC]
        terminal = start + idx
        halves = {_TERMINAL_SINGLE: 1, _TERMINAL_DOUBLE: 2}.get(rec[0], 0)
        for half in range(halves):
            status = _decode_zone_half(rec[2 + half * 4 : 6 + half * 4])
            if status is not None:
                out[terminal + half * half_offset] = status
    return out


@dataclass(frozen=True)
class NativeAreaStatus:
    """Live state of one partition, as reported by the status command."""

    mode: AreaMode
    alarm: bool
    alarm_memory: bool
    sabotage: bool = False


def decode_partition_statuses(resp: bytes) -> dict[int, NativeAreaStatus]:
    """Decode a partition-status response into ``{area_id: status}``.

    Unconfigured partitions (configured bit clear) and records with an unknown
    mode byte are skipped. A response shorter than the documented header plus
    all records raises ``ValueError`` rather than yielding partial data.

    Record byte 0 is the partition's live state as Prime/STUDIO reads it
    (``Comm::AreeRealTime``): 1 = in alarm, 2 = sabotage. ``alarm`` counts only
    while the partition is armed; the retained memory bit (byte 2, bit 0) never
    makes an area read as alarming by itself, and memory is set whenever either
    says so. Sabotage is reported whatever the arming state.
    """
    size = _PARTITION_REC * _PARTITION_COUNT
    if len(resp) < _STATUS_HEADER + size:
        raise ValueError(f"short partition-status response ({len(resp)} bytes)")
    data = resp[_STATUS_HEADER : _STATUS_HEADER + size]
    out: dict[int, NativeAreaStatus] = {}
    for area_id in range(_PARTITION_COUNT):
        flags, mode_byte, status = data[area_id * _PARTITION_REC : (area_id + 1) * _PARTITION_REC]
        if not status & _PARTITION_CONFIGURED:
            continue
        try:
            mode = AreaMode(mode_byte)
        except ValueError:
            continue
        active = flags == _AREA_STATE_ALARM
        out[area_id] = NativeAreaStatus(
            mode=mode,
            alarm=active and mode is not AreaMode.DISARMED,
            alarm_memory=active or bool(status & 0x01),
            sabotage=flags == _AREA_STATE_SABOTAGE,
        )
    return out


# System status (op 0x0D) reply, after the 4-byte echo: a 6-byte fault word, the
# panel flags, then the supply voltages in tenths of a volt. Bits as Prime/STUDIO
# shows them (Living_Monitoring_NEW::AggiornaPagina_Guasti) and as the panel's own
# fault string table orders them (libdriver); keys are the integration's fault
# flags (FAULT_FLAG_KEYS).
_SYSTEM_STATUS_LEN = 4 + 54
_SYSTEM_FAULT_WORD = 6
_SYSTEM_FAULT_BITS: dict[str, tuple[int, int]] = {
    "low_battery": (0, 2),
    "fault_mains": (0, 3),
    "no_phone_line": (0, 4),
    "jam_radio": (0, 5),
    "low_battery_wls": (0, 6),
    "disappearance_wls": (0, 7),
    "fault_gsm": (1, 0),
    "sensor_dirty": (1, 2),
    "zone_fault": (1, 3),
    "sirens": (1, 5),
    "power_supply": (1, 6),
    "radio_keypads": (1, 7),
    "tamper": (2, 5),
    "comm_internet": (2, 6),
}
# Tampers the panel also reports one by one (panel dislodged and opened,
# readers, keypads, expansions): any of them raises "tamper" too.
_SYSTEM_TAMPER_BITS: dict[str, tuple[int, int]] = {
    "panel_dislodged": (6, 0),
    "panel_open": (6, 1),
    "readers": (10, 6),
    "keypads": (10, 7),
    "expansions": (11, 0),
}
_SYSTEM_SERVICE = (6, 3)  # the SERV jumper: panel in service
_SYSTEM_PROGRAMMING = (6, 4)
_SYSTEM_VOLTE = (13, 0)  # the LTE communicator has VoLTE active
_SYSTEM_SUPPLY_V = 16  # switching supply output
_SYSTEM_BATTERY_V = 18


@dataclass(frozen=True)
class NativeSystemStatus:
    """Panel-wide live status (op 0x0D): faults, tampers and supply voltages."""

    faults: dict[str, bool]
    has_faults: bool
    supply_voltage: float
    battery_voltage: float
    service: bool
    programming: bool
    # The fault word and panel flags as read, for diagnostics.
    raw: bytes
    # Which device reports a tamper (keys of _SYSTEM_TAMPER_BITS); any of them
    # also raises the "tamper" fault.
    tamper_sources: dict[str, bool] = field(default_factory=dict)
    volte: bool = False


def decode_system_status(resp: bytes) -> NativeSystemStatus:
    """Decode a system-status (op 0x0D) response; a short one raises ``ValueError``."""
    if len(resp) < _SYSTEM_STATUS_LEN:
        raise ValueError(f"short system-status response ({len(resp)} bytes)")
    d = resp[4:_SYSTEM_STATUS_LEN]

    def bit(pos: tuple[int, int]) -> bool:
        return bool(d[pos[0]] >> pos[1] & 1)

    faults = {name: bit(pos) for name, pos in _SYSTEM_FAULT_BITS.items()}
    sources = {name: bit(pos) for name, pos in _SYSTEM_TAMPER_BITS.items()}
    faults["tamper"] = faults["tamper"] or any(sources.values())
    return NativeSystemStatus(
        faults={key: faults[key] for key in FAULT_FLAG_KEYS},
        has_faults=any(d[:_SYSTEM_FAULT_WORD]) or faults["tamper"],
        supply_voltage=int.from_bytes(d[_SYSTEM_SUPPLY_V : _SYSTEM_SUPPLY_V + 2], "little") / 10,
        battery_voltage=int.from_bytes(d[_SYSTEM_BATTERY_V : _SYSTEM_BATTERY_V + 2], "little")
        / 10,
        service=bit(_SYSTEM_SERVICE),
        programming=bit(_SYSTEM_PROGRAMMING),
        raw=bytes(d[:12]),
        tamper_sources=sources,
        volte=bit(_SYSTEM_VOLTE),
    )


# Power values (op 0x31) reply, after the 4-byte echo: u16 LE fields as
# Prime/STUDIO's Power page reads them (Living_Monitoring_NEW::LeggiTRPower);
# 0xFFFF = not reported. Checked on a live PrimeX 5.00 against that page.
_POWER_VALUES_LEN = 4 + 48
_POWER_BATTERY_TEMP = 6  # degrees Celsius
_POWER_BATTERY_RESISTANCE = 8  # milliohm
_POWER_NOT_REPORTED = 0xFFFF


@dataclass(frozen=True)
class NativePowerValues:
    """The battery readings of the power values (op 0x31); None when not reported."""

    battery_temperature: int | None
    battery_resistance: int | None  # milliohm


def decode_power_values(resp: bytes) -> NativePowerValues:
    """Decode a power-values (op 0x31) response; a short one raises ``ValueError``."""
    if len(resp) < _POWER_VALUES_LEN:
        raise ValueError(f"short power-values response ({len(resp)} bytes)")
    d = resp[4:_POWER_VALUES_LEN]

    def value(offset: int) -> int | None:
        raw = int.from_bytes(d[offset : offset + 2], "little")
        return None if raw == _POWER_NOT_REPORTED else raw

    return NativePowerValues(
        battery_temperature=value(_POWER_BATTERY_TEMP),
        battery_resistance=value(_POWER_BATTERY_RESISTANCE),
    )


# GSM status (op 0x5E) reply, after the 4-byte echo (Centrale6.dll
# Comm::LeggiInfoGSM4G): [result][fault bits][network bits][signal 0-100], then
# identifiers (IMSI at 34, ICCID at 54), the operator code (MCC+MNC, ASCII) at
# 78 and the modem's ATI text (with the IMEI) at 98. Only the first four bytes
# and the operator code are ever decoded: the identifiers are never kept.
_GSM_OPERATOR = slice(78, 98)
_GSM_MIN_LEN = 4 + _GSM_OPERATOR.stop
GSM_OK = 0
GSM_NO_MODULE = 2
GSM_NOT_INITIALISED = 3  # Prime/STUDIO: "check the SIM"
_GSM_FAULT_BITS = (
    "low_signal",
    "low_battery",
    "gprs",
    "sim_error",
    "no_signal",
    "no_operator",
    "module",
    "roaming",
)
_GSM_NETWORK_BITS = ((1, "2G"), (2, "3G"), (3, "4G"))
_GSM_OPERATOR_RE = re.compile(r"\d{5,6}")


@dataclass(frozen=True)
class NativeGsmStatus:
    """The LTE communicator status (op 0x5E), without any identifier."""

    # GSM_OK, GSM_NO_MODULE, GSM_NOT_INITIALISED, or another (error) code.
    result: int
    faults: dict[str, bool]
    networks: tuple[str, ...]
    signal: int  # 0-100
    # MCC+MNC (e.g. "22201"), "" when not registered.
    operator_code: str


def decode_gsm_status(resp: bytes) -> NativeGsmStatus:
    """Decode a GSM-status (op 0x5E) response; a short one raises ``ValueError``."""
    if len(resp) < _GSM_MIN_LEN:
        raise ValueError(f"short GSM-status response ({len(resp)} bytes)")
    result, faults, networks, signal = resp[4:8]
    operator = resp[4 + _GSM_OPERATOR.start : 4 + _GSM_OPERATOR.stop].split(b"\0")[0]
    code = operator.decode("ascii", "replace")
    return NativeGsmStatus(
        result=result,
        faults={name: bool(faults >> i & 1) for i, name in enumerate(_GSM_FAULT_BITS)},
        networks=tuple(name for i, name in _GSM_NETWORK_BITS if networks >> i & 1),
        signal=signal,
        operator_code=code if _GSM_OPERATOR_RE.fullmatch(code) else "",
    )


# Code check (op 0x04, "controllo_codice"/get_code_type) reply, after the 4-byte
# echo: [code type][code index + area-permission map]. The type byte is 0 for an
# unknown code and 0xFF for an empty field; a real code answers with its non-zero
# type (live on 5.00 a valid user code -> 0x01). The rest is the code's index and
# area-permission map, kept raw for a later per-area check but not decoded yet.
_CODE_CHECK_MIN_LEN = 4 + 1
_CODE_TYPE_NONE = frozenset({0x00, 0xFF})


@dataclass(frozen=True)
class NativeCodeCheck:
    """The result of a code check (op 0x04): whether the code is valid, plus raw."""

    valid: bool
    code_type: int
    raw: bytes  # the reply after the echo (code index + area-permission map)


def decode_code_check(resp: bytes) -> NativeCodeCheck:
    """Decode a code-check (op 0x04) response; a short one raises ``ValueError``.

    A code is valid when its type byte is neither 0 (unknown code) nor 0xFF
    (empty field).
    """
    if len(resp) < _CODE_CHECK_MIN_LEN:
        raise ValueError(f"short code-check response ({len(resp)} bytes)")
    code_type = resp[4]
    return NativeCodeCheck(
        valid=code_type not in _CODE_TYPE_NONE,
        code_type=code_type,
        raw=resp[4:],
    )


def decode_output_states(resp: bytes, start: int, end: int) -> dict[int, int]:
    """Decode a terminal-status response for ``[start, end)`` into ``{terminal: value}``.

    Only output terminals yield a value: record byte 1, 0 when the output is off
    and above 0 when it is on (Prime/STUDIO Living_Monitoring_NEW::
    DeterminaStatoUscita). A short response raises ``ValueError``.
    """
    count = end - start
    if len(resp) < _STATUS_HEADER + count * _TERMINAL_REC:
        raise ValueError(f"short terminal-status response ({len(resp)} bytes)")
    out: dict[int, int] = {}
    for idx in range(count):
        rec = resp[_STATUS_HEADER + idx * _TERMINAL_REC : _STATUS_HEADER + (idx + 1) * _TERMINAL_REC]
        if rec[0] == _TERMINAL_OUTPUT:
            out[start + idx] = rec[1]
    return out


# --------------------------------------------------------------------- decode
def decode_scene(modo6: bytes) -> dict[int, str]:
    """Decode the 6 MODO bytes of a scenario into ``{partition_index: mode}``.

    Each byte holds partition ``2i`` (low nibble) and ``2i+1`` (high nibble);
    nibble bits 1=away, 2=stay, 4=disarm, 0=untouched. Partitions left untouched
    are omitted. Multi-bit nibbles are joined with ``+``.
    """
    arms: dict[int, str] = {}
    for i, byte in enumerate(modo6):
        for nibble, pidx in ((byte & 0x0F, 2 * i), ((byte >> 4) & 0x0F, 2 * i + 1)):
            if nibble:
                arms[pidx] = (
                    "+".join(v for bit, v in _MODE_BIT.items() if nibble & bit) or f"0x{nibble:x}"
                )
    return arms


@dataclass(frozen=True)
class SceneDef:
    """A scenario's static definition: which partitions it sets, and to what."""

    id: int
    arms: dict[int, str]


@dataclass(frozen=True)
class NativeObject:
    """A labelled panel object (partition, arming scenario or output)."""

    id: int
    label: str


# Zone-kind codes, named as Prime/STUDIO shows them (Centrale5.dll:
# Living_Zone::GetTipoZona and GetTappIn, Living_ConfiguraTerminali::
# StructToFormZona; the same on 4.x and 5.x).
ZONE_TYPE_DELAYED = 1  # "Ritardata"
ZONE_TYPE_DELAYED_SHOWN = 2  # "Ritardata visualizzabile"
ZONE_TYPE_24H = 4  # "24 Ore"
ZONE_OPT_INTERIOR = 0x01  # "Interna"
ZONE_OPT_AUTO_BYPASS = 0x02  # "Autoescludibile": bypassed by itself when open at arming
ZONE_OPT_UNBYPASSABLE = 0x04  # "Non escludibile"
ZONE_OPT_CHIME = 0x08  # "Campanello"
ZONE_OPT1_HOLD_UP = 0x20  # "Rapina"
ZONE_OPT1_FAULT = 0x40  # "Zona guasto"
ZONE_OPT2_FIRE = 0x04  # "Incendio"
SENSOR_SHUTTER = 1  # "Tapparella"
SENSOR_SHOCK = 2  # "Inerziale"


@dataclass(frozen=True)
class NativeZoneDef:
    """A zone that exists on the panel, with its label and partitions.

    ``terminal`` is the physical terminal holding the zone (zone 1005 lives on
    terminal 0); ``areas`` are the partition ids from the settings bitmask.
    """

    id: int
    label: str
    terminal: int
    areas: tuple[int, ...]
    # Raw settings, None when not read: zone_cfg bytes 4 (zone type), 5 and 6
    # (options, options1) and 10 (pulses); the zone's zone_opt2 byte; the
    # terminal's terminal_cfg bytes 5 (balance), 6-8 (levels) and 11 (terminal
    # type), and its pulse_cfg detector type.
    zone_type: int | None = None
    options: int | None = None
    options1: int | None = None
    pulses: int | None = None
    options2: int | None = None
    balance: int | None = None
    levels: tuple[int, int, int] | None = None
    terminal_type: int | None = None
    sensor: int | None = None

    @property
    def auto_bypass(self) -> bool:
        """"Autoescludibile": the panel bypasses the zone itself when open at arming."""
        return bool((self.options or 0) & ZONE_OPT_AUTO_BYPASS)

    @property
    def bypassable(self) -> bool:
        """False for a "Non escludibile" zone (True while the options are unknown)."""
        return not (self.options or 0) & ZONE_OPT_UNBYPASSABLE

    @property
    def is_shutter(self) -> bool:
        """Roller-shutter detector ("Tapparella")."""
        return self.sensor == SENSOR_SHUTTER

    @property
    def is_shock(self) -> bool:
        """Shock / inertial detector ("Inerziale")."""
        return self.sensor == SENSOR_SHOCK

    @property
    def is_fire(self) -> bool:
        """Fire zone ("Incendio")."""
        return bool((self.options2 or 0) & ZONE_OPT2_FIRE)

    @property
    def is_24h(self) -> bool:
        """24-hour zone ("24 Ore"), active whatever the arming state."""
        return self.zone_type == ZONE_TYPE_24H

    @property
    def is_delayed(self) -> bool:
        """Zone with an entry delay ("Ritardata" / "Ritardata visualizzabile")."""
        return self.zone_type in (ZONE_TYPE_DELAYED, ZONE_TYPE_DELAYED_SHOWN)

    @property
    def is_interior(self) -> bool:
        """Interior zone ("Interna"), left out of partial arming."""
        return bool((self.options or 0) & ZONE_OPT_INTERIOR)

    @property
    def has_chime(self) -> bool:
        """Chime zone ("Campanello")."""
        return bool((self.options or 0) & ZONE_OPT_CHIME)

    @property
    def is_hold_up(self) -> bool:
        """Hold-up zone ("Rapina")."""
        return bool((self.options1 or 0) & ZONE_OPT1_HOLD_UP)

    @property
    def is_fault_zone(self) -> bool:
        """Fault zone ("Zona guasto")."""
        return bool((self.options1 or 0) & ZONE_OPT1_FAULT)


def _record(kinds: Mapping[str, tuple[int, bytes]], name: str, index: int, size: int) -> bytes | None:
    """Return record ``index`` of the ``name`` kind read, or None if not read."""
    entry = kinds.get(name)
    if entry is None:
        return None
    first, blob = entry
    rec = blob[(index - first) * size : (index - first + 1) * size]
    return rec if len(rec) == size else None


def _decode_zones(
    candidates: list[int],
    spans: list[tuple[int, int]],
    blobs: list[bytes],
    half_offset: int = SECOND_HALF_ZONE_OFFSET,
    kinds: Mapping[str, tuple[int, bytes]] | None = None,
) -> list[NativeZoneDef]:
    """Build the zones that exist from the label/settings reads of each span.

    ``blobs`` holds ``[labels, settings]`` per span, in span order. A candidate
    zone with an empty partition bitmask is not in use and is dropped.
    ``kinds`` maps each zone-kind read that was made (``opt2_<span index>``,
    ``terminal``, ``pulse``) to ``(first zone or terminal, data)``.
    """
    kinds = kinds or {}
    zones: list[NativeZoneDef] = []
    for span, ((first, last), labels, settings) in enumerate(
        zip(spans, blobs[0::2], blobs[1::2], strict=True)
    ):
        for zid in candidates:
            if not first <= zid <= last:
                continue
            idx = zid - first
            rec = settings[idx * _ZONE_CFG_REC : (idx + 1) * _ZONE_CFG_REC]
            mask = int.from_bytes(rec[:_ZONE_MASK_LEN], "little")
            areas = tuple(b for b in range(_PARTITION_COUNT) if mask & (1 << b))
            if not areas:
                continue
            terminal = zone_terminal(zid, half_offset)[0]
            zone = NativeZoneDef(
                id=zid, label=_label_at(labels, idx), terminal=terminal, areas=areas
            )
            if len(rec) == _ZONE_CFG_REC:
                zone = replace(
                    zone, zone_type=rec[4], options=rec[5], options1=rec[6], pulses=rec[10]
                )
            if (opt2 := _record(kinds, f"opt2_{span}", zid, 1)) is not None:
                zone = replace(zone, options2=opt2[0])
            if (trec := _record(kinds, "terminal", terminal, _TERMINAL_CFG_REC)) is not None:
                zone = replace(
                    zone,
                    balance=trec[5],
                    levels=(trec[6], trec[7], trec[8]),
                    terminal_type=trec[11],
                )
            if (prec := _record(kinds, "pulse", terminal, _PULSE_CFG_REC)) is not None:
                zone = replace(zone, sensor=prec[_PULSE_SENSOR])
            zones.append(zone)
    # Back to terminal order (terminal, then half).
    order = {zid: pos for pos, zid in enumerate(candidates)}
    return sorted(zones, key=lambda z: order[z.id])


@dataclass(frozen=True)
class Local6004Structure:
    """Which objects exist on the panel, and their labels (read once, natively).

    * areas: partitions with the configured bit set in the partition status;
    * zones: zones whose terminal is single (half A) or double (halves A and
      B) and whose partition bitmask is non-zero;
    * scenarios: arming scenarios whose label is not the factory default;
    * outputs: panel output terminals (1005..1009) of the output type, keyed
      by terminal id.

    On a live PrimeX 4.07 this reproduces the panel's own area, zone and
    scenario lists and labels exactly. A kind is None when its read failed
    (e.g. a panel variant rejecting part of the terminal scan).
    """

    areas: list[NativeObject] | None = None
    zones: list[NativeZoneDef] | None = None
    scenarios: list[NativeObject] | None = None
    outputs: list[NativeObject] | None = None


@dataclass(frozen=True)
class Local6004Config:
    """Static config read once from the panel over TCP 6004 (read-only)."""

    firmware: str
    layout_ok: bool  # offsets valid (the firmware family is in _LAYOUTS)
    scenes: list[SceneDef] = field(default_factory=list)
    zone_areas: dict[int, list[int]] = field(default_factory=dict)
    # None when it could not be read (unsupported layout, or the structure
    # read failed).
    structure: Local6004Structure | None = None


def scene_is_active(arms: dict[int, str], areas_by_id: dict[int, AreaMode]) -> bool:
    """Return True iff every partition the scene targets matches its mode now.

    ``areas_by_id`` maps area id -> live :class:`AreaMode`. A scene with no
    targets is never "active".
    """
    if not arms:
        return False
    for pidx, mode in arms.items():
        want = _MODE_TO_AREAMODE.get(mode)
        if want is None or areas_by_id.get(pidx) != want:
            return False
    return True


def scene_target_modes(arms: dict[int, str]) -> dict[int, AreaMode] | None:
    """Return a scene's targets as ``{partition: AreaMode}``.

    None unless every target maps cleanly (away -> TOTAL, stay -> PARTIAL,
    disarm -> DISARMED): a combined or unknown nibble has no single mode, and
    a scene with no targets sets nothing.
    """
    modes: dict[int, AreaMode] = {}
    for pidx, mode in arms.items():
        want = _MODE_TO_AREAMODE.get(mode)
        if want is None:
            return None
        modes[pidx] = want
    return modes or None


@dataclass(frozen=True)
class EventClass:
    """One class of the panel's event codes: the codes from ``start`` on.

    ``kind`` is the 1-based object kind byte (see event_types.EVENT_OBJECT_KINDS),
    0 when the class names no object.
    """

    start: int
    kind: int


def logger_class_table(resp: bytes) -> tuple[int, int] | None:
    """Return the event-class table's (address, length) from an op 0x1F reply.

    None when the reply does not hold a plausible table: the log is then
    decoded without event names.
    """
    if len(resp) < 12:
        return None
    addr = int.from_bytes(resp[4:8], "little")
    length = int.from_bytes(resp[8:12], "little")
    if not addr or not 0 < length <= _LOG_CLASS_MAX_LEN or length % _LOG_CLASS_REC:
        return None
    return addr, length


def decode_event_classes(table: bytes) -> list[EventClass]:
    """Decode the event-class table; the last class returned is the end sentinel.

    The table is read up to its first entry that does not start after the one
    before (the sentinel repeats its code, then padding follows).
    """
    classes: list[EventClass] = []
    for off in range(0, len(table) - _LOG_CLASS_REC + 1, _LOG_CLASS_REC):
        start = int.from_bytes(table[off : off + 2], "little")
        if classes and start <= classes[-1].start:
            break
        classes.append(EventClass(start=start, kind=table[off + 3]))
    return classes


def decode_event_log(
    blob: bytes,
    area_labels: dict[int, str] | None = None,
    scenario_labels: dict[int, str] | None = None,
    classes: list[EventClass] | None = None,
    zone_labels: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """Decode the panel event-log ring (14-byte records) into a time-sorted list.

    Record: ``[ts u32 LE, epoch 2000-01-01 = panel local time][b4 partition
    bitmask][b5..7][event code u16 LE][b10][b11 flags: 0x80=set /
    clear=restoral][..]``. Empty/invalid slots (ts outside 2025-2027) are
    skipped. The code is named with ``classes`` (the panel's class table):
    the event type, and for a class spanning several codes the object it names
    (zone, area, scenario: by label; anything else: by 1-based number). Without
    a class for it the code is shown raw (``code:<b8><b9>``).
    ``area_labels`` / ``scenario_labels`` / ``zone_labels`` map ids -> names.
    """
    area_labels = area_labels or {}
    scenario_labels = scenario_labels or {}
    labels = {"area": area_labels, "scenario": scenario_labels, "zone": zone_labels or {}}
    classes = classes or []
    starts = [c.start for c in classes]
    out: list[dict[str, Any]] = []
    for off in range(0, len(blob) - _LOG_REC + 1, _LOG_REC):
        r = blob[off : off + _LOG_REC]
        ts = int.from_bytes(r[0:4], "little")
        if not (0x30000000 < ts < 0x40000000):
            continue
        b0, b1, b3 = r[8], r[9], r[11]
        partitions = [
            area_labels.get(p, f"area{p + 1}") for p in range(8) if r[4] & (1 << p)
        ]
        entry: dict[str, Any] = {
            "time": (_EPOCH2000 + dt.timedelta(seconds=ts)).strftime("%Y-%m-%d %H:%M:%S"),
            "event": f"code:{b0:02x}{b1:02x}",
            "partitions": partitions,
            "restoral": not (b3 & 0x80),
        }
        code = b0 | b1 << 8
        index = bisect.bisect_right(starts, code) - 1
        if 0 <= index < len(classes) - 1:  # the last class is the end sentinel
            event_class = classes[index]
            entry["event"] = (
                EVENT_TYPE_NAMES[index]
                if index < len(EVENT_TYPE_NAMES)
                else f"Event type {index}"
            )
            span = classes[index + 1].start - event_class.start
            if span > 1 and 1 <= event_class.kind <= len(EVENT_OBJECT_KINDS):
                kind = EVENT_OBJECT_KINDS[event_class.kind - 1]
                number = code - event_class.start
                named = labels.get(kind)
                entry[kind] = named.get(number, str(number + 1)) if named is not None else number + 1
        out.append(entry)
    out.sort(key=lambda e: e["time"])
    return out


class Local6004Error(Exception):
    """Any failure talking the local 6004 protocol (connect/read/decrypt)."""


class NativeCommandError(Local6004Error):
    """A native write command failed; see the subclasses for what is known."""

    def __init__(self, op: int, detail: str) -> None:
        self.op = op
        super().__init__(f"native command {op}: {detail}")


class NativeCommandNotSent(NativeCommandError):
    """The command certainly never left: nothing was written to the panel.

    Raised only for failures before the command frame was handed to the
    socket (e.g. the connection could not be opened), so retrying it on
    another channel cannot execute it twice.
    """


class NativeCommandInvalid(NativeCommandNotSent):
    """The client refused the arguments (id out of range, bad PIN); nothing sent."""


class NativeZonesNotReady(NativeCommandNotSent):
    """The live pre-check found a zone of the target areas not ready; nothing sent."""


class NativeCommandUncertain(NativeCommandError):
    """The command was sent, but its outcome is unknown.

    It may have been executed (e.g. a timeout or a dropped connection while
    waiting for the answer), so it must not be retried blindly.
    """


class NativeCommandRejected(NativeCommandUncertain):
    """The panel answered, but not with the expected opcode echo.

    Whether the panel executed the command is not known (the response codes
    are undocumented), so this is treated as uncertain as well.
    """


def _validated(op: int, build: Callable[[], bytes]) -> bytes:
    """Run a frame builder, turning its argument errors into NativeCommandInvalid.

    Only argument checks raise ValueError here, before any connection is
    touched, so the caller may safely use another channel instead.
    """
    try:
        return build()
    except ValueError as err:
        raise NativeCommandInvalid(op, str(err)) from err


class _Progress:
    """Whether a command frame was handed to the socket (survives a timeout)."""

    sent = False


class Local6004Client:
    """Async client for the panel's TCP 6004 protocol.

    Read-only unless one of the write methods (``async_set_area_modes``,
    ``async_set_zone_bypass``, ``async_set_output``, ``async_reset_areas``) is
    called.
    """

    def __init__(self, host: str, password: str, *, port: int = PORT, timeout: float = 10.0):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._key, self._iv = make_key_iv(password)
        # Memory map for this panel's firmware family. Defaults to 40x so the
        # live status/command methods work even before a config read; a config
        # read updates it from the firmware version (see _read_config).
        self._layout = _LAYOUT_40X
        # Persistent connection for the frequent status command; opened lazily,
        # dropped on any error and re-opened on the next call.
        self._status_conn: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        self._status_lock = asyncio.Lock()

    @property
    def supports_native_writes(self) -> bool:
        """Whether native write commands are verified for this panel's firmware.

        False on a family whose write-command response is not yet verified on a
        live panel: commands are then refused.
        """
        return self._layout.native_writes_ok

    async def _live[T](self, step: Awaitable[T], timeout: float) -> T:
        """Run one live status read under the status lock and its own budget.

        A failure closes the persistent connection (the next read reopens it)
        and is raised as :class:`Local6004Error`.
        """
        async with self._status_lock:
            try:
                return await asyncio.wait_for(step, timeout)
            except (TimeoutError, OSError, ValueError, asyncio.IncompleteReadError) as err:
                await self._close_status_conn()
                raise Local6004Error(str(err) or type(err).__name__) from err

    async def async_get_area_statuses(
        self, timeout: float = STATUS_TIMEOUT
    ) -> dict[int, NativeAreaStatus]:
        """Return live partition statuses over a persistent connection (read-only)."""
        return await self._live(self._get_area_statuses(), timeout)

    async def _get_area_statuses(self) -> dict[int, NativeAreaStatus]:
        return decode_partition_statuses(await self._status(_status_cmd(_OP_PARTITION_STATUS)))

    async def async_get_system_status(self, timeout: float = STATUS_TIMEOUT) -> NativeSystemStatus:
        """Return the live system status: faults, tampers, supply voltages (read-only)."""
        return await self._live(self._get_system_status(), timeout)

    async def _get_system_status(self) -> NativeSystemStatus:
        return decode_system_status(await self._status(_status_cmd(_OP_SYSTEM_STATUS)))

    async def async_get_power_values(self, timeout: float = STATUS_TIMEOUT) -> NativePowerValues:
        """Return the battery temperature and internal resistance (read-only)."""
        return await self._live(self._get_power_values(), timeout)

    async def _get_power_values(self) -> NativePowerValues:
        return decode_power_values(await self._status(_status_cmd(_OP_POWER_VALUES)))

    async def async_get_gsm_status(self, timeout: float = STATUS_TIMEOUT) -> NativeGsmStatus:
        """Return the LTE communicator status, without identifiers (read-only)."""
        return await self._live(self._get_gsm_status(), timeout)

    async def _get_gsm_status(self) -> NativeGsmStatus:
        return decode_gsm_status(await self._status(_status_cmd(_OP_GSM_STATUS)))

    async def async_check_code(
        self, code: str, timeout: float = STATUS_TIMEOUT
    ) -> NativeCodeCheck:
        """Verify a user code against the panel, returning its validity (read-only).

        Sends op 0x04 ("controllo_codice"): the panel checks the code and answers
        with its type. Nothing is armed, disarmed or written. The code is only
        used to build the frame; it is never stored or logged. A code that is not
        1-6 digits raises :class:`NativeCommandInvalid` before any I/O.
        """
        body = _validated(_OP_CHECK_CODE, lambda: check_code_cmd(code))
        return await self._live(self._check_code(body), timeout)

    async def _check_code(self, body: bytes) -> NativeCodeCheck:
        return decode_code_check(await self._status(body))

    async def async_get_output_states(
        self, terminals: set[int], timeout: float = STATUS_TIMEOUT
    ) -> dict[int, int]:
        """Return the live value of the output ``terminals`` (0 = off, read-only)."""
        return await self._live(self._get_output_states(terminals), timeout)

    async def _get_output_states(self, terminals: set[int]) -> dict[int, int]:
        out: dict[int, int] = {}
        for start, end in terminal_chunks(terminals):
            data = start.to_bytes(2, "little") + end.to_bytes(2, "little")
            resp = await self._status(_status_cmd(_OP_TERMINAL_STATUS, data))
            out |= decode_output_states(resp, start, end)
        return {terminal: value for terminal, value in out.items() if terminal in terminals}

    async def async_get_zone_statuses(
        self, zone_ids: set[int], timeout: float = STATUS_TIMEOUT
    ) -> dict[int, NativeZoneStatus]:
        """Return live statuses for ``zone_ids`` over the persistent connection."""
        return await self._live(self._get_zone_statuses(zone_ids), timeout)

    async def _get_zone_statuses(self, zone_ids: set[int]) -> dict[int, NativeZoneStatus]:
        half = self._layout.half_offset
        out: dict[int, NativeZoneStatus] = {}
        for start, end in terminal_chunks({zone_terminal(z, half)[0] for z in zone_ids}):
            data = start.to_bytes(2, "little") + end.to_bytes(2, "little")
            resp = await self._status(_status_cmd(_OP_TERMINAL_STATUS, data))
            out |= decode_terminal_statuses(resp, start, end, half)
        return {zone_id: status for zone_id, status in out.items() if zone_id in zone_ids}

    async def _status(self, body: bytes) -> bytes:
        if self._status_conn is None:
            self._status_conn = await asyncio.open_connection(self._host, self._port)
        reader, writer = self._status_conn
        return await self._xfer(reader, writer, body, first=True)

    # ------------------------------------------------------------ write commands
    async def async_set_area_modes(
        self,
        modes: Mapping[int, AreaMode],
        *,
        require_ready: Collection[int] = (),
        pin: str | None = None,
        timeout: float = COMMAND_TIMEOUT,
    ) -> None:
        """Set several partitions' arming modes in one command (op 3).

        Partitions not in ``modes`` are left alone, so this also applies an
        arming scenario's targets at once.

        ``require_ready`` zones are read live in the same locked exchange,
        right before sending: if one is neither ready nor bypassed (or not
        reported), :class:`NativeZonesNotReady` is raised and nothing is
        sent. How the panel treats a native arm with open zones is unknown.
        """
        data = _validated(_OP_SET_ARMING, lambda: arming_data(modes))
        await self._command(_OP_SET_ARMING, data, pin, timeout, set(require_ready))

    async def async_set_zone_bypass(
        self,
        zone_id: int,
        excluded: bool,
        *,
        pin: str | None = None,
        timeout: float = COMMAND_TIMEOUT,
    ) -> None:
        """Bypass (exclude) or un-bypass a zone (op 9)."""
        half = self._layout.half_offset
        data = _validated(_OP_SET_ZONE_BYPASS, lambda: zone_bypass_data(zone_id, excluded, half))
        await self._command(_OP_SET_ZONE_BYPASS, data, pin, timeout)

    async def async_set_output(
        self,
        terminal: int,
        on: bool,
        *,
        pin: str | None = None,
        timeout: float = COMMAND_TIMEOUT,
    ) -> None:
        """Turn a panel output (terminal 1005..1009) on or off (op 8)."""
        data = _validated(
            _OP_SET_OUTPUT, lambda: output_data(terminal, on, self._layout.output_terminals)
        )
        await self._command(_OP_SET_OUTPUT, data, pin, timeout)

    async def async_reset_areas(
        self,
        area_ids: Iterable[int],
        *,
        pin: str | None = None,
        timeout: float = COMMAND_TIMEOUT,
    ) -> None:
        """Reset the partitions' alarm memory (op 16)."""
        data = _validated(_OP_RESET_PARTITIONS, lambda: reset_data(area_ids))
        await self._command(_OP_RESET_PARTITIONS, data, pin, timeout)

    async def async_clear_calls(
        self,
        area_ids: Iterable[int],
        *,
        pin: str | None = None,
        timeout: float = COMMAND_TIMEOUT,
    ) -> None:
        """Clear the panel's outgoing call queue (op 0x39, action 2).

        Pending calls and reports are dropped and an ongoing call is ended;
        nothing else changes (no alarm, output or memory is touched).
        """
        data = _validated(_OP_STOP_ACTIONS, lambda: clear_calls_data(area_ids))
        await self._command(_OP_STOP_ACTIONS, data, pin, timeout)

    async def _command(
        self,
        op: int,
        data: bytes,
        pin: str | None,
        timeout: float,
        require_ready: set[int] | None = None,
    ) -> None:
        """Send one write command over the persistent connection, once.

        Never retried here. A failure raises :class:`NativeCommandNotSent` (or
        a subclass) when the frame was never handed to the socket, and
        :class:`NativeCommandUncertain` (or its subclass
        :class:`NativeCommandRejected`) once it may have reached the panel.
        """
        body = _validated(op, lambda: _write_cmd(op, data, pin))
        progress = _Progress()
        async with self._status_lock:
            try:
                resp = await asyncio.wait_for(
                    self._send_command(op, body, progress, require_ready or set()), timeout
                )
            except _IO_ERRORS as err:
                await self._close_status_conn()
                detail = str(err) or type(err).__name__
                if progress.sent:
                    raise NativeCommandUncertain(op, detail) from err
                raise NativeCommandNotSent(op, detail) from err
            except asyncio.CancelledError:
                # A cancelled exchange leaves the stream mid-frame.
                self._drop_status_conn()
                raise
        _LOGGER.debug("Native command %d answered %s", op, resp[:_STATUS_HEADER].hex(" "))
        check_command_response(op, resp)

    async def _send_command(
        self, op: int, body: bytes, progress: _Progress, require_ready: set[int]
    ) -> bytes:
        if self._status_conn is not None:
            # Prove the kept-open connection is alive with a read-only status
            # read first: a write into a connection the panel already dropped
            # would leave its outcome unknown, a failed read is harmless.
            # Bounded on its own, so a stalled connection is replaced while
            # the command budget still covers a reconnect and the write.
            try:
                await asyncio.wait_for(
                    self._status(_status_cmd(_OP_PARTITION_STATUS)), STATUS_TIMEOUT
                )
            except _IO_ERRORS:
                await self._close_status_conn()
        if self._status_conn is None:
            self._status_conn = await asyncio.open_connection(self._host, self._port)
        if require_ready:
            live = await self._get_zone_statuses(require_ready)
            blocked = sorted(
                zone_id
                for zone_id in require_ready
                if (status := live.get(zone_id)) is None
                or not (status.excluded or status.state is ZoneState.READY)
            )
            if blocked:
                raise NativeZonesNotReady(op, f"zones not ready: {blocked}")
        reader, writer = self._status_conn
        # From here on the panel may receive the command.
        progress.sent = True
        return await self._xfer(reader, writer, body, first=True)

    async def async_close(self) -> None:
        """Close the persistent status connection, if open."""
        async with self._status_lock:
            await self._close_status_conn()

    def _drop_status_conn(self) -> None:
        """Forget the persistent connection and close it without waiting."""
        if self._status_conn is not None:
            self._status_conn[1].close()
            self._status_conn = None

    async def _close_status_conn(self) -> None:
        if self._status_conn is None:
            return
        _, writer = self._status_conn
        self._status_conn = None
        writer.close()
        # Bounded: a stalled socket must not keep holding the status lock.
        try:
            await asyncio.wait_for(writer.wait_closed(), _CLOSE_TIMEOUT)
        except (OSError, TimeoutError):
            pass

    async def async_read_config(self) -> Local6004Config:
        """Connect, read the static config (read-only), and disconnect.

        The firmware and scenario definitions are required: a failure raises
        :class:`Local6004Error`. The structure is best effort, per object kind
        and outside that budget: it covers the whole terminal range, verified
        on one firmware only, so a failed step leaves its kinds None.
        """
        try:
            config = await asyncio.wait_for(self._read_config(), self._timeout)
        except _STEP_ERRORS as err:
            raise Local6004Error(str(err) or type(err).__name__) from err
        if not config.layout_ok:
            return config
        structure = await self._read_structure()
        return replace(
            config,
            zone_areas={z.id: list(z.areas) for z in structure.zones or []},
            structure=structure,
        )

    async def async_read_event_log(
        self,
        area_labels: dict[int, str] | None = None,
        scenario_labels: dict[int, str] | None = None,
        zone_labels: dict[int, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Read and decode the panel event log (read-only), time-sorted.

        The panel's event-class table is read in the same session, as
        Prime/STUDIO does, so the codes are named on every firmware family.
        """
        log_addr = self._layout.log_addr
        if log_addr is None:
            raise Local6004Error(
                f"event log offset not mapped for the {self._layout.name} firmware layout"
            )
        try:
            blob, table = await asyncio.wait_for(self._read_log(log_addr), self._timeout)
        except (TimeoutError, OSError, ValueError) as err:
            raise Local6004Error(str(err)) from err
        classes = decode_event_classes(table) if table is not None else []
        return decode_event_log(blob, area_labels, scenario_labels, classes, zone_labels)

    async def _read_log(self, log_addr: int) -> tuple[bytes, bytes | None]:
        """Read the event log and the panel's event-class table, in one session."""
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            rules = await self._xfer(reader, writer, _OPEN_LOG, first=True)
            span = logger_class_table(rules)
            table = await self._read(reader, writer, *span) if span is not None else None
            return await self._read(reader, writer, log_addr, _LOG_LEN), table
        finally:
            await self._close_quietly(writer)

    async def _read_config(self) -> Local6004Config:
        # version first (its own context) -> gate on the 40x layout
        ver_raw = (await self._session(_OPEN_VER, [(_VERSION_ADDR, 16)]))[0]
        firmware = ver_raw.split(b"\x00")[0].decode("latin-1").strip()
        layout = _LAYOUTS.get(firmware[:1])
        if layout is None:
            # Known-reachable panel, but its config memory map is not mapped:
            # the panel is not supported.
            return Local6004Config(firmware=firmware, layout_ok=False)
        self._layout = layout

        (modi,) = await self._session(
            _OPEN_CFG, [(layout.scenario_modi, _SCENARIO_MODI_REC * _SCENARIO_COUNT)]
        )
        scenes = []
        for sid in range(_SCENARIO_COUNT):
            rec = modi[sid * _SCENARIO_MODI_REC : sid * _SCENARIO_MODI_REC + _SCENE_MODO_BYTES]
            arms = decode_scene(rec)
            if arms:  # skip undefined scenarios
                scenes.append(SceneDef(id=sid, arms=arms))
        return Local6004Config(firmware=firmware, layout_ok=True, scenes=scenes)

    async def _read_structure(self) -> Local6004Structure:
        """Read which objects exist and their labels (see :class:`Local6004Structure`).

        Three best-effort steps, each on its own connection and budget: the
        partition scan (areas), the terminal scan (zones and outputs), then
        the labels and zone settings (everything). A failed step only drops
        the kinds that depend on it.
        """
        layout = self._layout
        area_ids = await self._best_effort(self._scan_partitions())
        terminal_types = await self._best_effort(self._scan_terminals())
        # The zone->area settings offset is only known for some layouts; without
        # it no zone is decoded (the native zone *status* still works).
        decode_zones = layout.zone_cfg is not None
        candidates = (
            zone_candidates(terminal_types, layout.half_offset)
            if terminal_types is not None and decode_zones
            else []
        )
        spans = _half_spans(candidates, layout.half_offset)
        reads = [
            (layout.area_labels, _LABEL_REC * _PARTITION_COUNT),
            (layout.scenario_labels, _LABEL_REC * _SCENARIO_COUNT),
            (layout.output_labels, _LABEL_REC * len(layout.output_terminals)),
        ]
        zone_cfg = layout.zone_cfg
        if zone_cfg is not None:  # equivalently: decode_zones, so spans is non-empty
            for first, last in spans:
                count = last - first + 1
                reads.append((layout.zone_labels + first * _LABEL_REC, count * _LABEL_REC))
                reads.append((zone_cfg + first * _ZONE_CFG_REC, count * _ZONE_CFG_REC))
        # Zone-kind inputs (see NativeZoneDef): each zone's third option byte, per
        # span, and the settings and detector type of the terminals holding the
        # zones, one contiguous read each. A table whose offset is not mapped is
        # skipped and its fields stay None.
        kind_reads: dict[str, tuple[int, int, int]] = {}  # name -> (first, addr, length)
        if layout.zone_opt2 is not None:
            for span, (first, last) in enumerate(spans):
                kind_reads[f"opt2_{span}"] = (first, layout.zone_opt2 + first, last - first + 1)
        zone_terminals = sorted({zone_terminal(z, layout.half_offset)[0] for z in candidates})
        if zone_terminals:
            first_t = zone_terminals[0]
            count_t = zone_terminals[-1] - first_t + 1
            for name, base, size in (
                ("terminal", layout.terminal_cfg, _TERMINAL_CFG_REC),
                ("pulse", layout.pulse_cfg, _PULSE_CFG_REC),
            ):
                if base is not None:
                    kind_reads[name] = (first_t, base + first_t * size, count_t * size)
        reads += [(addr, length) for _, addr, length in kind_reads.values()]
        blobs = await self._best_effort(self._session(_OPEN_CFG, reads))
        if blobs is None:
            return Local6004Structure()
        area_labels, scenario_labels, output_labels, *rest = blobs
        zone_blobs = rest[: len(rest) - len(kind_reads)]
        kinds = {
            name: (first, blob)
            for (name, (first, _, _)), blob in zip(
                kind_reads.items(), rest[len(zone_blobs) :], strict=True
            )
        }

        scenarios = [
            NativeObject(id=sid, label=_label_at(scenario_labels, sid))
            for sid in range(_SCENARIO_COUNT)
        ]
        return Local6004Structure(
            areas=(
                None
                if area_ids is None
                else [NativeObject(id=a, label=_label_at(area_labels, a)) for a in area_ids]
            ),
            zones=(
                _decode_zones(candidates, spans, zone_blobs, layout.half_offset, kinds)
                if terminal_types is not None and decode_zones
                else None
            ),
            scenarios=[s for s in scenarios if not is_factory_default_scenario(s.id, s.label)],
            outputs=(
                None
                if terminal_types is None
                else [
                    NativeObject(
                        id=t, label=_label_at(output_labels, t - layout.output_terminals.start)
                    )
                    for t in layout.output_terminals
                    if terminal_types.get(t) == _TERMINAL_OUTPUT
                ]
            ),
        )

    @staticmethod
    async def _best_effort[T](step: Awaitable[T]) -> T | None:
        """Run one structure step under its own budget; None if it fails."""
        try:
            return await asyncio.wait_for(step, STRUCTURE_TIMEOUT)
        except _STEP_ERRORS:
            return None

    async def _scan_partitions(self) -> list[int]:
        """Return the configured partitions (read-only status command, no context op)."""
        (resp,) = await self._command_session([(_OP_PARTITION_STATUS, b"")])
        return decode_configured_partitions(resp)

    async def _scan_terminals(self) -> dict[int, int]:
        """Return every terminal's type (read-only status commands, no context op).

        Any failed or short chunk fails the whole scan: a partial zone list
        would be worse than none.
        """
        count = self._layout.terminal_count
        chunks = [
            (start, min(start + _TERMINAL_CHUNK, count))
            for start in range(0, count, _TERMINAL_CHUNK)
        ]
        resps = await self._command_session(
            [
                (_OP_TERMINAL_STATUS, s.to_bytes(2, "little") + e.to_bytes(2, "little"))
                for s, e in chunks
            ]
        )
        terminal_types: dict[int, int] = {}
        for (start, end), resp in zip(chunks, resps, strict=True):
            terminal_types |= decode_terminal_types(resp, start, end)
        return terminal_types

    async def _command_session(self, commands: list[tuple[int, bytes]]) -> list[bytes]:
        """Open one connection, send ``(op, data)`` status commands, return responses.

        Every body goes through :func:`_status_cmd`, so only the read-only
        status opcodes can be sent.
        """
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            return [
                await self._xfer(reader, writer, _status_cmd(op, data), first=True)
                for op, data in commands
            ]
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _session(self, open_payload: bytes, reads: list[tuple[int, int]]) -> list[bytes]:
        """Open one connection, send the context op, perform reads, return data."""
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            await self._xfer(reader, writer, open_payload, first=True)  # context ACK
            out = []
            for addr, length in reads:
                out.append(await self._read(reader, writer, addr, length))
            return out
        finally:
            await self._close_quietly(writer)

    @staticmethod
    async def _close_quietly(writer: asyncio.StreamWriter) -> None:
        """Close a session's connection; a teardown error changes nothing."""
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    async def _read(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        addr: int,
        length: int,
    ) -> bytes:
        out = bytearray()
        first = True
        while len(out) < length:
            want = min(_CHUNK, length - len(out))
            resp = await self._xfer(
                reader, writer, _read_cmd(addr + len(out), want, cont=not first), first=False
            )
            out += resp[:want]  # drop trailing status byte(s)
            first = False
        return bytes(out)

    async def _xfer(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        app: bytes,
        *,
        first: bool,
    ) -> bytes:
        writer.write(_build_frame(app, self._key, self._iv, first=first))
        await writer.drain()
        header = await reader.readexactly(10)
        if header[:2] not in _RESPONSE_PREAMBLES:
            raise ValueError(f"bad preamble {header[:2].hex()} (wrong password?)")
        ln = int.from_bytes(header[6:8], "little")
        ct = await reader.readexactly(ln - 10)
        return _unpad(_aes(self._key, self._iv, ct, decrypt=True))
