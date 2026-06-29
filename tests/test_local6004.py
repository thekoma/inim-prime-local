"""Unit tests for the read-only TCP 6004 client (no Home Assistant, no socket)."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.inim_prime.client import local6004 as m
from custom_components.inim_prime.client.const import AreaMode

_KEY, _IV = m.make_key_iv("pass")


# --------------------------------------------------------------- pure helpers
def test_crc16_arc_known_vector() -> None:
    # CRC-16/ARC of "123456789" is the standard check value 0xBB3D.
    assert m.crc16_arc(b"123456789") == 0xBB3D


def test_make_key_iv() -> None:
    key, iv = m.make_key_iv("pass")
    assert key == b"pass".ljust(16, b"\x00")
    assert iv == bytes(key[i] ^ i for i in range(16))


def test_pad_unpad_roundtrip() -> None:
    for n in (0, 1, 15, 16, 17):
        data = bytes(range(n % 256))[:n]
        assert m._unpad(m._pad(data)) == data
    # padded length is always a multiple of 16 and never zero-length
    assert len(m._pad(b"")) == 16


def test_build_frame_roundtrip_and_crc() -> None:
    app = m._read_cmd(0x14030D20, 160, cont=False)
    frame = m._build_frame(app, _KEY, _IV, first=False)
    assert frame[:2] == m._PREAMBLE
    # CRC is over [+4..end] and matches the stored little-endian field
    assert int.from_bytes(frame[2:4], "little") == m.crc16_arc(frame[4:])
    # LEN field equals the whole frame length
    assert int.from_bytes(frame[6:8], "little") == len(frame)
    # decrypting the ciphertext recovers the app payload
    ct = frame[10:]
    assert m._unpad(m._aes(_KEY, _IV, ct, decrypt=True)) == app


def test_build_frame_first_flag() -> None:
    assert m._build_frame(b"\x17", _KEY, _IV, first=True)[4:6] == b"\x01\x00"
    assert m._build_frame(b"\x17", _KEY, _IV, first=False)[4:6] == b"\x00\x00"


def test_read_cmd_byte_exact() -> None:
    # Verified against a real capture: read addr 0x1a002400 len 12.
    assert m._read_cmd(0x1A002400, 12, cont=False).hex() == (
        "0024001a000000000c0000000c00000000001167"
    )
    # checksum = sum of the first 19 bytes & 0xff
    cmd = m._read_cmd(0x1407DFA8, 950, cont=False)
    assert cmd[-1] == sum(cmd[:-1]) & 0xFF


def test_read_cmd_opcodes() -> None:
    assert m._read_cmd(0x10, 4, cont=False)[18] == m._READ_START
    assert m._read_cmd(0x10, 4, cont=True)[18] == m._READ_CONT


@pytest.mark.parametrize(
    ("modo", "expected"),
    [
        (bytes([0x11, 0x01, 0, 0, 0, 0]), {0: "away", 1: "away", 2: "away"}),
        (bytes([0x44, 0x04, 0, 0, 0, 0]), {0: "disarm", 1: "disarm", 2: "disarm"}),
        (bytes([0x00, 0x10, 0, 0, 0, 0]), {3: "away"}),
        (bytes([0x22, 0x00, 0, 0, 0, 0]), {0: "stay", 1: "stay"}),
        (bytes(6), {}),  # undefined scenario -> no targets
    ],
)
def test_decode_scene(modo: bytes, expected: dict[int, str]) -> None:
    assert m.decode_scene(modo) == expected


def test_decode_scene_combo_nibble() -> None:
    # nibble 3 == away+stay (multi-bit); unknown nibble 8 -> hex fallback
    assert m.decode_scene(bytes([0x03, 0x80, 0, 0, 0, 0])) == {0: "away+stay", 3: "0x8"}


def test_scene_is_active() -> None:
    arms = {0: "away", 2: "disarm"}
    assert m.scene_is_active(arms, {0: AreaMode.TOTAL, 2: AreaMode.DISARMED})
    assert not m.scene_is_active(arms, {0: AreaMode.TOTAL, 2: AreaMode.TOTAL})
    assert not m.scene_is_active(arms, {0: AreaMode.TOTAL})  # missing area
    assert not m.scene_is_active({}, {0: AreaMode.TOTAL})  # empty never active
    # unmappable mode (e.g. a combo) is never "active"
    assert not m.scene_is_active({0: "away+stay"}, {0: AreaMode.TOTAL})


# --------------------------------------------------------------- async client
def _resp(plaintext: bytes) -> bytes:
    """Build a response frame the way the panel would (reuses the same crypto)."""
    return m._build_frame(plaintext, _KEY, _IV, first=False)


class _FakeReader:
    def __init__(self, data: bytes) -> None:
        self._buf = data
        self._pos = 0

    async def readexactly(self, n: int) -> bytes:
        chunk = self._buf[self._pos : self._pos + n]
        if len(chunk) < n:
            raise asyncio.IncompleteReadError(chunk, n)
        self._pos += n
        return chunk


class _FakeWriter:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.sent.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def _patch_sessions(monkeypatch: pytest.MonkeyPatch, sessions: list[list[bytes]]) -> list:
    """Make asyncio.open_connection serve one pre-scripted fake stream per call."""
    pairs = [(_FakeReader(b"".join(frames)), _FakeWriter()) for frames in sessions]
    it = iter(pairs)

    async def fake_open(host: str, port: int):  # noqa: ANN202
        return next(it)

    monkeypatch.setattr(m.asyncio, "open_connection", fake_open)
    return pairs


def _version_frame(text: str) -> bytes:
    return _resp(text.encode("latin-1").ljust(16, b"\x00"))


def _modi_frame() -> bytes:
    modi = bytearray(m._SCENARIO_MODI_REC * m._SCENARIO_COUNT)
    modi[0:6] = bytes([0x11, 0x01, 0, 0, 0, 0])  # scenario 0: part 0,1,2 away
    return _resp(bytes(modi))


def _zone_frames() -> list[bytes]:
    zone = bytearray(m._ZONE_CFG_REC * m._ZONE_COUNT)
    zone[0] = 0x01  # zone 0 -> area 0
    zone[m._ZONE_CFG_REC] = 0x06  # zone 1 -> areas 1 and 2
    blob = bytes(zone)
    # the client reads in 1024-byte chunks
    return [_resp(blob[i : i + 1024]) for i in range(0, len(blob), 1024)]


async def test_async_read_config_success(monkeypatch: pytest.MonkeyPatch) -> None:
    ack = _resp(b"\x00\x00\x00\x00")
    _patch_sessions(
        monkeypatch,
        [
            [ack, _version_frame("4.07 PX020")],
            [ack, _modi_frame(), *_zone_frames()],
        ],
    )
    cfg = await m.Local6004Client("host", "pass").async_read_config()
    assert cfg.layout_ok
    assert cfg.firmware == "4.07 PX020"
    assert [s.id for s in cfg.scenes] == [0]
    assert cfg.scenes[0].arms == {0: "away", 1: "away", 2: "away"}
    assert cfg.zone_areas[0] == [0]
    assert cfg.zone_areas[1] == [1, 2]


async def test_async_read_config_wrong_firmware(monkeypatch: pytest.MonkeyPatch) -> None:
    ack = _resp(b"\x00\x00\x00\x00")
    _patch_sessions(monkeypatch, [[ack, _version_frame("3.10 PRIME")]])
    cfg = await m.Local6004Client("host", "pass").async_read_config()
    assert not cfg.layout_ok
    assert cfg.firmware == "3.10 PRIME"
    assert cfg.scenes == []


async def test_async_read_config_bad_preamble(monkeypatch: pytest.MonkeyPatch) -> None:
    # A frame with the wrong preamble (wrong password) -> Local6004Error.
    bad = b"\xaa\xaa" + _resp(b"\x00")[2:]
    _patch_sessions(monkeypatch, [[bad]])
    with pytest.raises(m.Local6004Error):
        await m.Local6004Client("host", "nope").async_read_config()


async def test_async_read_config_connect_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(host: str, port: int):  # noqa: ANN202
        raise OSError("refused")

    monkeypatch.setattr(m.asyncio, "open_connection", boom)
    with pytest.raises(m.Local6004Error):
        await m.Local6004Client("host", "pass").async_read_config()


async def test_session_swallows_wait_closed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wait_closed() error during teardown must not fail the read."""

    class _RaisingWriter(_FakeWriter):
        async def wait_closed(self) -> None:
            raise OSError("already closed")

    reader = _FakeReader(b"".join([_resp(b"\x00\x00\x00\x00"), _version_frame("3.0 X")]))
    writer = _RaisingWriter()

    async def fake_open(host: str, port: int):  # noqa: ANN202
        return reader, writer

    monkeypatch.setattr(m.asyncio, "open_connection", fake_open)
    cfg = await m.Local6004Client("host", "pass").async_read_config()
    assert cfg.firmware == "3.0 X"  # completed despite the teardown error


# --------------------------------------------------------------- event log
def _log_record(ts: int, partmask: int, b0: int, b1: int, b3: int) -> bytes:
    """Build a 14-byte event-log record."""
    r = bytearray(14)
    r[0:4] = ts.to_bytes(4, "little")
    r[4] = partmask
    r[8] = b0
    r[9] = b1
    r[11] = b3
    return bytes(r)


def test_decode_event_log() -> None:
    blob = (
        _log_record(0x31000000, 0x01, 0x6E, 0x1F, 0x80)  # Valid key, part0, set
        + _log_record(0x31000010, 0x00, 0x5E, 0x22, 0x00)  # Scenario idx 3, restoral
        + _log_record(0x31000020, 0x04, 0xAA, 0xBB, 0x80)  # unknown code, part2
        + _log_record(0, 0, 0, 0, 0)  # empty slot -> skipped
    )
    events = m.decode_event_log(blob, {0: "Home"}, {3: "Dis.Box"})
    assert len(events) == 3  # the empty slot is dropped
    assert events[0]["event"] == "Valid key"
    assert events[0]["partitions"] == ["Home"]
    assert events[0]["restoral"] is False  # 0x80 = set
    assert "scenario" not in events[0]
    assert events[1]["event"] == "Scenario"
    assert events[1]["scenario"] == "Dis.Box"
    assert events[1]["restoral"] is True  # flag clear
    # unknown code renders raw; unlabeled partition falls back to areaN
    assert events[2]["event"] == "code:aabb"
    assert events[2]["partitions"] == ["area3"]


async def test_async_read_event_log(monkeypatch: pytest.MonkeyPatch) -> None:
    blob = _log_record(0x31000000, 0x01, 0x6E, 0x1F, 0x80)
    client = m.Local6004Client("host", "pass")

    async def fake_session(open_payload, reads):  # noqa: ANN001, ANN202
        assert open_payload == m._OPEN_LOG
        assert reads == [(m._LOG_ADDR, m._LOG_LEN)]
        return [blob]

    monkeypatch.setattr(client, "_session", fake_session)
    events = await client.async_read_event_log({0: "Home"})
    assert events[0]["event"] == "Valid key"


async def test_async_read_event_log_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = m.Local6004Client("host", "pass")

    async def boom(open_payload, reads):  # noqa: ANN001, ANN202
        raise OSError("down")

    monkeypatch.setattr(client, "_session", boom)
    with pytest.raises(m.Local6004Error):
        await client.async_read_event_log()
