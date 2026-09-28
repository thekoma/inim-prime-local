"""Client for the INIM PrimeX local protocol (TCP 6004).

A companion to the cgi HTTP client: read-only by default, with four opt-in
write commands (see "Write commands" below). What the local protocol adds:

* **static structure** — which partitions, zones, arming scenarios and outputs
  exist, and their labels, read once at setup (see :class:`Local6004Structure`).
  It also names the panel outputs correctly, which the cgi does not.
* **multi-active scenes** — each arming scenario's per-partition target mode
  (``away``/``stay``/``disarm``); a scenario is "active" when every partition it
  targets currently matches that mode (computed against live cgi area state).
* **zone -> area** mapping.

Protocol (reverse-engineered + verified on a live capture; AES key = the panel
LAN password): each frame is ``50 50 | CRC16-ARC(LE over [+4..end]) | flag(LE) |
LEN(LE total) | 00 00 | AES-128-CBC ciphertext``. A connection opens with a
context op-code; reads use op 0x11 (start) / 0x10 (continue) with
``[addr:4 LE][0000:4][len:4 LE][len:4 LE][00 00][op][chk=sum(prev19)&0xff]``.

⚠️ Opcode allow-lists: this module only ever emits the two memory-read
opcodes, the read-only *status* commands (op 6 partition statuses, op 7
terminal statuses) and the four *write* commands below. It never builds a
memory-write/program frame (the same framing with another opcode — on a
production panel a stray write could brick it). ``_read_cmd``,
``_status_cmd`` and ``_write_cmd`` each check their opcode against their own
literal allow-list and raise :class:`OpcodeNotAllowed` otherwise
(:class:`ReadOnlyViolation` for the read-only ones; an explicit check, not an
``assert``, so ``python -O`` cannot drop it).

Write commands (same source, not yet live-verified by this project): request
``[op:4 LE][pin:6][data]``, where the PIN is one digit per byte padded with
0xFF (``74 00 00 00 00 00`` = no PIN):

* op 3 set arming status: 30 bytes, one per partition, holding the target
  :class:`AreaMode` (0 = leave that partition alone);
* op 8 set output: ``[terminal:2 LE][1 on | 0 off:2 LE]``;
* op 9 set zone bypass: ``[zone:2 LE][0 bypass | 2 unbypass:2 LE]``;
* op 16 reset partitions (alarm memory): ``[partition bitmask:4 LE]``.

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
``[alarm flags][AreaMode][0x10 configured | 0x01 alarm memory]``. It answers in
~10 ms even when the cgi takes seconds, so it is the fast path for area state.

Live terminal status (op 7, same source) takes ``[start:2 LE][end:2 LE]``
(end exclusive, at most 20 terminals) and answers ``[header:18][10 bytes x 20]``.
A record is ``[type][00][zone A: flags, 00, ZoneState, 00][zone B: ...]`` with
type 0 = single zone, 3 = double zone, 1 = output, 4 = disabled; flags
``0x10`` = excluded (bypassed), ``0x01`` = alarm memory. Zone ``n`` (< 1005) is
half A of terminal ``n``; zone ``n + 1005`` is half B of terminal ``n`` (verified
against the cgi on a PrimeX).

The static structure uses the label tables and the zone-settings layout also
documented by Pitscheider's library: 16-byte ASCII label records per
partition, zone (indexed by zone id), arming scenario and output (indexed by
``terminal - 1005``), and 11-byte zone settings whose first 4 bytes are the
uint32 LE partition bitmask.

The EEPROM config offsets below are the **40x** layout (PrimeX firmware 4.x),
which is a compile-time-constant memory map in the official client. They are
NOT valid for other firmware families, so callers must gate on the firmware
major version (see :attr:`Local6004Config.layout_ok`).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from collections.abc import Awaitable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import AreaMode, ZoneState

_LOGGER = logging.getLogger(__name__)

PORT = 6004
_PREAMBLE = b"\x50\x50"
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

# The only status (read-only command) opcodes ever sent. Literal, so a renamed
# or mistyped constant above cannot widen it.
_STATUS_OPS = frozenset({6, 7})

# Write commands (Pitscheider's CommandOperation). Sent only by the
# async_set_*/async_reset_* methods, which the integration calls only when the
# user enabled native commands.
_OP_SET_ARMING = 3
_OP_SET_OUTPUT = 8
_OP_SET_ZONE_BYPASS = 9
_OP_RESET_PARTITIONS = 16
# The only state-changing opcodes ever sent (literal, see _STATUS_OPS).
_WRITE_OPS = frozenset({3, 8, 9, 16})
_PIN_LEN = 6
_PIN_PAD = 0xFF
_OUTPUT_ON = 1
_OUTPUT_OFF = 0
# Pitscheider: bypass -> 0, un-bypass -> 2 (1 is not used there).
_BYPASS_ON = 0
_BYPASS_OFF = 2
_ZONE_ID_COUNT = 2 * SECOND_HALF_ZONE_OFFSET
# Bytes of the response header that echo the opcode (as its bitwise NOT).
_ECHO_LEN = 4
# Per-command ceiling for a write: a liveness read (itself capped at
# STATUS_TIMEOUT) plus the write, each of which can take ~4 s on a cold
# channel. A timeout after the write was sent leaves its outcome unknown, so
# this is generous.
COMMAND_TIMEOUT = 10.0
# I/O failures of one exchange on the persistent connection.
_IO_ERRORS = (TimeoutError, OSError, ValueError, asyncio.IncompleteReadError)

# Event log (40x): ring of 14-byte records at 0xA1D0.
_LOG_ADDR = 0xA1D0
_LOG_LEN = 56000
_LOG_REC = 14
_EPOCH2000 = dt.datetime(2000, 1, 1)

# Event-code dictionary (record bytes B0,B1 -> human label), built by correlating
# a live 6004 log with the panel's own CSV export. Unknown codes render raw.
_EVENT_CODES: dict[str, str] = {
    "0422": "Failed call",
    "0522": "Failed call",
    "fc22": "Call queue full",
    "6e1f": "Valid key",
    "6d1f": "Valid key",
    "891c": "Reset partition",
    "8a1c": "Reset partition",
    "8b1c": "Reset partition",
    "8c1c": "Reset partition",
    "2f1c": "Partition armed (away)",
    "301c": "Partition armed (away)",
    "311c": "Partition armed (away)",
    "321c": "Partition armed (away)",
    "6b1c": "Disarm partition",
    "6c1c": "Disarm partition",
    "6d1c": "Disarm partition",
    "6e1c": "Disarm partition",
    "210c": "Bypass zone",
    "0f10": "Bypass zone",
    "1e10": "Bypass zone",
    "5b22": "Scenario",
    "5c22": "Scenario",
    "5d22": "Scenario",
    "5e22": "Scenario",
    "5f22": "Scenario",
    "6022": "Scenario",
    "6222": "Scenario",
}

# 40x (PrimeX 4.x) EEPROM offsets (base 0x14000000). Firmware-version-specific.
_VERSION_ADDR = 0x1A002400
_SCENARIO_MODI_ADDR = 0x1407DFA8
_SCENARIO_MODI_REC = 19
_SCENARIO_COUNT = 50
_ZONE_CFG_ADDR = 0x14073414
_ZONE_CFG_REC = 11
_ZONE_MASK_LEN = 4
_LABEL_REC = 16
_AREA_LABELS_ADDR = 0x14030D20
_ZONE_LABELS_ADDR = 0x14030F00
_SCENARIO_LABELS_ADDR = 0x1403D8E0
_OUTPUT_LABELS_ADDR = 0x1403DEB0

# Factory-default scenario names carry their 1-based index: "Scenario 10" or
# "SCENARIO   031". Unused scenarios keep them; the cgi does not list those.
_FACTORY_SCENARIO_RE = re.compile(r"(?:Scenario|SCENARIO)\s+(\d+)")

# Scenario MODO nibble bits -> live AreaMode they require.
_MODE_BIT = {0x01: "away", 0x02: "stay", 0x04: "disarm"}
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
    return op.to_bytes(4, "little") + _NO_PIN + data


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


def zone_bypass_data(zone_id: int, excluded: bool) -> bytes:
    """Encode op 9 data: ``[zone:2 LE][0 bypass | 2 unbypass:2 LE]``."""
    if not 0 <= zone_id < _ZONE_ID_COUNT:
        raise ValueError(f"zone id {zone_id} out of range")
    value = _BYPASS_ON if excluded else _BYPASS_OFF
    return zone_id.to_bytes(2, "little") + value.to_bytes(2, "little")


def output_data(terminal: int, on: bool) -> bytes:
    """Encode op 8 data: ``[terminal:2 LE][1 on | 0 off:2 LE]``."""
    if terminal not in _OUTPUT_TERMINALS:
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


def zone_terminal(zone_id: int) -> tuple[int, int]:
    """Return ``(terminal, half)`` holding a zone (half 0 = A, 1 = B)."""
    if zone_id >= SECOND_HALF_ZONE_OFFSET:
        return zone_id - SECOND_HALF_ZONE_OFFSET, 1
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


def zone_candidates(terminal_types: dict[int, int]) -> list[int]:
    """Return the zone ids the terminal types allow, in the cgi's order.

    A single-zone terminal holds zone ``n`` (half A); a double-zone terminal
    also holds ``n + 1005`` (half B). Outputs, disabled and unknown terminals
    hold no zone. The order (terminal, then half) matches the cgi listing.
    """
    zones: list[int] = []
    for terminal in sorted(t for t in terminal_types if t < SECOND_HALF_ZONE_OFFSET):
        halves = {_TERMINAL_SINGLE: 1, _TERMINAL_DOUBLE: 2}.get(terminal_types[terminal], 0)
        zones.extend(terminal + half * SECOND_HALF_ZONE_OFFSET for half in range(halves))
    return zones


def _half_spans(zone_ids: list[int]) -> list[tuple[int, int]]:
    """Cover ``zone_ids`` with one ``[first, last]`` span per zone half.

    Labels and settings are indexed by zone id, so two contiguous reads fetch
    everything without reading the whole 2010-zone tables.
    """
    spans: list[tuple[int, int]] = []
    for half in (
        [z for z in zone_ids if z < SECOND_HALF_ZONE_OFFSET],
        [z for z in zone_ids if z >= SECOND_HALF_ZONE_OFFSET],
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


def decode_terminal_statuses(resp: bytes, start: int, end: int) -> dict[int, NativeZoneStatus]:
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
                out[terminal + half * SECOND_HALF_ZONE_OFFSET] = status
    return out


@dataclass(frozen=True)
class NativeAreaStatus:
    """Live state of one partition, as reported by the status command."""

    mode: AreaMode
    alarm: bool
    alarm_memory: bool


def decode_partition_statuses(resp: bytes) -> dict[int, NativeAreaStatus]:
    """Decode a partition-status response into ``{area_id: status}``.

    Unconfigured partitions (configured bit clear) and records with an unknown
    mode byte are skipped. A response shorter than the documented header plus
    all records raises ``ValueError`` rather than yielding partial data.

    ``alarm`` comes from the active alarm flag (record byte 0) only; the
    retained memory bit (byte 2) never makes an area read as alarming by
    itself. Memory is set whenever either bit is set.
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
        active = bool(flags & 0x01)
        out[area_id] = NativeAreaStatus(
            mode=mode,
            alarm=active and mode is not AreaMode.DISARMED,
            alarm_memory=active or bool(status & 0x01),
        )
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


def _decode_zones(
    candidates: list[int], spans: list[tuple[int, int]], blobs: list[bytes]
) -> list[NativeZoneDef]:
    """Build the zones that exist from the label/settings reads of each span.

    ``blobs`` holds ``[labels, settings]`` per span, in span order. A candidate
    zone with an empty partition bitmask is not in use and is dropped.
    """
    zones: list[NativeZoneDef] = []
    for (first, last), labels, settings in zip(spans, blobs[0::2], blobs[1::2], strict=True):
        for zid in candidates:
            if not first <= zid <= last:
                continue
            idx = zid - first
            off = idx * _ZONE_CFG_REC
            mask = int.from_bytes(settings[off : off + _ZONE_MASK_LEN], "little")
            areas = tuple(b for b in range(_PARTITION_COUNT) if mask & (1 << b))
            if areas:
                zones.append(
                    NativeZoneDef(
                        id=zid,
                        label=_label_at(labels, idx),
                        terminal=zone_terminal(zid)[0],
                        areas=areas,
                    )
                )
    # Back to the cgi's order (terminal, then half).
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
      by terminal id, which is also the id the cgi uses.

    On a live PrimeX 4.07 this reproduces the cgi's area, zone and scenario
    sets and labels exactly. A kind is None when its read failed (e.g. a
    panel variant rejecting part of the terminal scan): the cgi's list is then
    used for that kind.
    """

    areas: list[NativeObject] | None = None
    zones: list[NativeZoneDef] | None = None
    scenarios: list[NativeObject] | None = None
    outputs: list[NativeObject] | None = None


@dataclass(frozen=True)
class Local6004Config:
    """Static config read once from the panel over TCP 6004 (read-only)."""

    firmware: str
    layout_ok: bool  # offsets valid (firmware major == 4 / 40x layout)
    scenes: list[SceneDef] = field(default_factory=list)
    zone_areas: dict[int, list[int]] = field(default_factory=dict)
    # None when it could not be read (unsupported layout, or the structure
    # read failed): the cgi structure is then used as is.
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


def decode_event_log(
    blob: bytes,
    area_labels: dict[int, str] | None = None,
    scenario_labels: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """Decode the panel event-log ring (14-byte records) into a time-sorted list.

    Record: ``[ts u32 LE, epoch 2000-01-01 = panel local time][b4 partition
    bitmask][b5..7][B0 subtype][B1 class][B2 source][B3 flags: 0x80=set /
    clear=restoral][..]``. Empty/invalid slots (ts outside 2025-2027) are skipped.
    ``area_labels`` / ``scenario_labels`` map ids -> names for enrichment.
    """
    area_labels = area_labels or {}
    scenario_labels = scenario_labels or {}
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
            "event": _EVENT_CODES.get(f"{b0:02x}{b1:02x}", f"code:{b0:02x}{b1:02x}"),
            "partitions": partitions,
            "restoral": not (b3 & 0x80),
        }
        if b1 == 0x22 and b0 >= 0x5B and (b0 - 0x5B) in scenario_labels:
            entry["scenario"] = scenario_labels[b0 - 0x5B]
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
        # Persistent connection for the frequent status command; opened lazily,
        # dropped on any error and re-opened on the next call.
        self._status_conn: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        self._status_lock = asyncio.Lock()

    async def async_get_area_statuses(
        self, timeout: float = STATUS_TIMEOUT
    ) -> dict[int, NativeAreaStatus]:
        """Return live partition statuses over a persistent connection (read-only)."""
        async with self._status_lock:
            try:
                return await asyncio.wait_for(self._get_area_statuses(), timeout)
            except (TimeoutError, OSError, ValueError, asyncio.IncompleteReadError) as err:
                await self._close_status_conn()
                raise Local6004Error(str(err) or type(err).__name__) from err

    async def _get_area_statuses(self) -> dict[int, NativeAreaStatus]:
        return decode_partition_statuses(await self._status(_status_cmd(_OP_PARTITION_STATUS)))

    async def async_get_zone_statuses(
        self, zone_ids: set[int], timeout: float = STATUS_TIMEOUT
    ) -> dict[int, NativeZoneStatus]:
        """Return live statuses for ``zone_ids`` over the persistent connection."""
        async with self._status_lock:
            try:
                return await asyncio.wait_for(self._get_zone_statuses(zone_ids), timeout)
            except (TimeoutError, OSError, ValueError, asyncio.IncompleteReadError) as err:
                await self._close_status_conn()
                raise Local6004Error(str(err) or type(err).__name__) from err

    async def _get_zone_statuses(self, zone_ids: set[int]) -> dict[int, NativeZoneStatus]:
        out: dict[int, NativeZoneStatus] = {}
        for start, end in terminal_chunks({zone_terminal(z)[0] for z in zone_ids}):
            data = start.to_bytes(2, "little") + end.to_bytes(2, "little")
            resp = await self._status(_status_cmd(_OP_TERMINAL_STATUS, data))
            out |= decode_terminal_statuses(resp, start, end)
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
        pin: str | None = None,
        timeout: float = COMMAND_TIMEOUT,
    ) -> None:
        """Set several partitions' arming modes in one command (op 3).

        Partitions not in ``modes`` are left alone, so this also applies an
        arming scenario's targets at once.
        """
        await self._command(_OP_SET_ARMING, arming_data(modes), pin, timeout)

    async def async_set_zone_bypass(
        self,
        zone_id: int,
        excluded: bool,
        *,
        pin: str | None = None,
        timeout: float = COMMAND_TIMEOUT,
    ) -> None:
        """Bypass (exclude) or un-bypass a zone (op 9)."""
        await self._command(_OP_SET_ZONE_BYPASS, zone_bypass_data(zone_id, excluded), pin, timeout)

    async def async_set_output(
        self,
        terminal: int,
        on: bool,
        *,
        pin: str | None = None,
        timeout: float = COMMAND_TIMEOUT,
    ) -> None:
        """Turn a panel output (terminal 1005..1009) on or off (op 8)."""
        await self._command(_OP_SET_OUTPUT, output_data(terminal, on), pin, timeout)

    async def async_reset_areas(
        self,
        area_ids: Iterable[int],
        *,
        pin: str | None = None,
        timeout: float = COMMAND_TIMEOUT,
    ) -> None:
        """Reset the partitions' alarm memory (op 16)."""
        await self._command(_OP_RESET_PARTITIONS, reset_data(area_ids), pin, timeout)

    async def _command(self, op: int, data: bytes, pin: str | None, timeout: float) -> None:
        """Send one write command over the persistent connection, once.

        Never retried here. A failure raises :class:`NativeCommandNotSent` when
        the frame was never handed to the socket, and
        :class:`NativeCommandUncertain` (or its subclass
        :class:`NativeCommandRejected`) once it may have reached the panel.
        """
        body = _write_cmd(op, data, pin)
        progress = _Progress()
        async with self._status_lock:
            try:
                resp = await asyncio.wait_for(self._send_command(body, progress), timeout)
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

    async def _send_command(self, body: bytes, progress: _Progress) -> bytes:
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
        on one firmware only, so a failed step leaves its kinds None and the
        caller keeps the cgi's list for them.
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
    ) -> list[dict[str, Any]]:
        """Read and decode the panel event log (read-only), time-sorted."""
        try:
            blob = (
                await asyncio.wait_for(
                    self._session(_OPEN_LOG, [(_LOG_ADDR, _LOG_LEN)]), self._timeout
                )
            )[0]
        except (TimeoutError, OSError, ValueError) as err:
            raise Local6004Error(str(err)) from err
        return decode_event_log(blob, area_labels, scenario_labels)

    async def _read_config(self) -> Local6004Config:
        # version first (its own context) -> gate on the 40x layout
        ver_raw = (await self._session(_OPEN_VER, [(_VERSION_ADDR, 16)]))[0]
        firmware = ver_raw.split(b"\x00")[0].decode("latin-1").strip()
        layout_ok = firmware[:1] == "4"
        if not layout_ok:
            return Local6004Config(firmware=firmware, layout_ok=False)

        (modi,) = await self._session(
            _OPEN_CFG, [(_SCENARIO_MODI_ADDR, _SCENARIO_MODI_REC * _SCENARIO_COUNT)]
        )
        scenes = []
        for sid in range(_SCENARIO_COUNT):
            rec = modi[sid * _SCENARIO_MODI_REC : sid * _SCENARIO_MODI_REC + 6]
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
        area_ids = await self._best_effort(self._scan_partitions())
        terminal_types = await self._best_effort(self._scan_terminals())
        candidates = zone_candidates(terminal_types) if terminal_types is not None else []
        spans = _half_spans(candidates)
        reads = [
            (_AREA_LABELS_ADDR, _LABEL_REC * _PARTITION_COUNT),
            (_SCENARIO_LABELS_ADDR, _LABEL_REC * _SCENARIO_COUNT),
            (_OUTPUT_LABELS_ADDR, _LABEL_REC * len(_OUTPUT_TERMINALS)),
        ]
        for first, last in spans:
            count = last - first + 1
            reads.append((_ZONE_LABELS_ADDR + first * _LABEL_REC, count * _LABEL_REC))
            reads.append((_ZONE_CFG_ADDR + first * _ZONE_CFG_REC, count * _ZONE_CFG_REC))
        blobs = await self._best_effort(self._session(_OPEN_CFG, reads))
        if blobs is None:
            return Local6004Structure()
        area_labels, scenario_labels, output_labels, *zone_blobs = blobs

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
                None if terminal_types is None else _decode_zones(candidates, spans, zone_blobs)
            ),
            scenarios=[s for s in scenarios if not is_factory_default_scenario(s.id, s.label)],
            outputs=(
                None
                if terminal_types is None
                else [
                    NativeObject(
                        id=t, label=_label_at(output_labels, t - _OUTPUT_TERMINALS.start)
                    )
                    for t in _OUTPUT_TERMINALS
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
        would be worse than falling back to the cgi's.
        """
        chunks = [
            (start, min(start + _TERMINAL_CHUNK, _TERMINAL_COUNT))
            for start in range(0, _TERMINAL_COUNT, _TERMINAL_CHUNK)
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
        if header[:2] != _PREAMBLE:
            raise ValueError(f"bad preamble {header[:2].hex()} (wrong password?)")
        ln = int.from_bytes(header[6:8], "little")
        ct = await reader.readexactly(ln - 10)
        return _unpad(_aes(self._key, self._iv, ct, decrypt=True))
