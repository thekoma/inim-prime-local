"""Read-only client for the INIM PrimeX local protocol (TCP 6004).

This is a strictly READ-ONLY companion to the cgi HTTP client. The cgi already
provides live state and entity names; what the local protocol adds is data the
cgi cannot express:

* **multi-active scenes** — each arming scenario's per-partition target mode
  (``away``/``stay``/``disarm``); a scenario is "active" when every partition it
  targets currently matches that mode (computed against live cgi area state).
* **zone -> area** mapping.

Protocol (reverse-engineered + verified on a live capture; AES key = the panel
LAN password): each frame is ``50 50 | CRC16-ARC(LE over [+4..end]) | flag(LE) |
LEN(LE total) | 00 00 | AES-128-CBC ciphertext``. A connection opens with a
context op-code; reads use op 0x11 (start) / 0x10 (continue) with
``[addr:4 LE][0000:4][len:4 LE][len:4 LE][00 00][op][chk=sum(prev19)&0xff]``.

⚠️ READ-ONLY: this module only ever emits the two read opcodes. It never builds
a write/program frame (a write is the same framing with a write opcode — on a
production panel a stray write could brick it). ``_read_cmd`` asserts the opcode.

The EEPROM config offsets below are the **40x** layout (PrimeX firmware 4.x),
which is a compile-time-constant memory map in the official client. They are
NOT valid for other firmware families, so callers must gate on the firmware
major version (see :attr:`Local6004Config.layout_ok`).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import AreaMode

PORT = 6004
_PREAMBLE = b"\x50\x50"
_READ_START = 0x11
_READ_CONT = 0x10
_CHUNK = 1024

# Connection-open context op-codes.
_OPEN_CFG = b"\x17\x00\x00\x00\x00"  # 0x140xxxxx config region
_OPEN_VER = b"\x0d\x00\x00\x00\x00"  # version region
_OPEN_LOG = b"\x1f\x00\x00\x00\x00"  # low-address event-log region

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
_ZONE_COUNT = 100

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


def _read_cmd(addr: int, length: int, *, cont: bool) -> bytes:
    op = _READ_CONT if cont else _READ_START
    assert op in (_READ_START, _READ_CONT), "READ-ONLY guard"
    body = bytearray()
    body += addr.to_bytes(4, "little")
    body += b"\x00\x00\x00\x00"
    body += length.to_bytes(4, "little")
    body += length.to_bytes(4, "little")
    body += b"\x00\x00"
    body += bytes([op])
    body += bytes([sum(body) & 0xFF])
    return bytes(body)


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
class Local6004Config:
    """Static config read once from the panel over TCP 6004 (read-only)."""

    firmware: str
    layout_ok: bool  # offsets valid (firmware major == 4 / 40x layout)
    scenes: list[SceneDef] = field(default_factory=list)
    zone_areas: dict[int, list[int]] = field(default_factory=dict)


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


class Local6004Client:
    """Async, strictly read-only client for the panel's TCP 6004 protocol."""

    def __init__(self, host: str, password: str, *, port: int = PORT, timeout: float = 10.0):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._key, self._iv = make_key_iv(password)

    async def async_read_config(self) -> Local6004Config:
        """Connect, read the static config (read-only), and disconnect."""
        try:
            return await asyncio.wait_for(self._read_config(), self._timeout)
        except (TimeoutError, OSError, ValueError) as err:
            raise Local6004Error(str(err)) from err

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

        modi, zcfg = await self._session(
            _OPEN_CFG,
            [
                (_SCENARIO_MODI_ADDR, _SCENARIO_MODI_REC * _SCENARIO_COUNT),
                (_ZONE_CFG_ADDR, _ZONE_CFG_REC * _ZONE_COUNT),
            ],
        )
        scenes = []
        for sid in range(_SCENARIO_COUNT):
            rec = modi[sid * _SCENARIO_MODI_REC : sid * _SCENARIO_MODI_REC + 6]
            arms = decode_scene(rec)
            if arms:  # skip undefined scenarios
                scenes.append(SceneDef(id=sid, arms=arms))
        zone_areas: dict[int, list[int]] = {}
        for zid in range(_ZONE_COUNT):
            off = zid * _ZONE_CFG_REC
            if off < len(zcfg):
                mask = zcfg[off]
                areas = [b for b in range(8) if mask & (1 << b)]
                if areas:
                    zone_areas[zid] = areas
        return Local6004Config(
            firmware=firmware, layout_ok=True, scenes=scenes, zone_areas=zone_areas
        )

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
