"""Unit tests for the read-only TCP 6004 client (no Home Assistant, no socket)."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.inim_prime.client import local6004 as m
from custom_components.inim_prime.client.const import AreaMode, ZoneState

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


# --------------------------------------------------------------- partition status
def _status_resp(records: dict[int, bytes]) -> bytes:
    """Build a partition-status response frame (18-byte header + 30 records)."""
    data = bytearray(m._PARTITION_REC * m._PARTITION_COUNT)
    for area_id, rec in records.items():
        data[area_id * 3 : area_id * 3 + 3] = rec
    return _resp(bytes(m._STATUS_HEADER) + bytes(data))


def test_status_cmd_bytes_and_guard() -> None:
    assert m._status_cmd(6) == b"\x06\x00\x00\x00\x74\x00\x00\x00\x00\x00"
    with pytest.raises(AssertionError):
        m._status_cmd(3)  # SET_ARMING_STATUS must never be built


def test_decode_partition_statuses() -> None:
    header = bytes(m._STATUS_HEADER)
    data = bytearray(m._PARTITION_REC * m._PARTITION_COUNT)
    data[0:3] = b"\x00\x04\x10"  # area 0: disarmed
    data[3:6] = b"\x00\x00\x00"  # area 1: not configured
    data[6:9] = b"\x01\x01\x11"  # area 2: armed away, alarm memory -> alarm
    data[9:12] = b"\x00\x01\x10"  # area 3: armed away
    data[12:15] = b"\x00\x09\x10"  # area 4: unknown mode -> skipped
    data[15:18] = b"\x01\x04\x11"  # area 5: disarmed with memory -> no active alarm
    data[18:21] = b"\x00\x01\x11"  # area 6: armed, retained memory only -> no alarm
    out = m.decode_partition_statuses(header + bytes(data))
    assert set(out) == {0, 2, 3, 5, 6}
    assert out[6] == m.NativeAreaStatus(mode=AreaMode.TOTAL, alarm=False, alarm_memory=True)
    assert out[0] == m.NativeAreaStatus(mode=AreaMode.DISARMED, alarm=False, alarm_memory=False)
    assert out[2] == m.NativeAreaStatus(mode=AreaMode.TOTAL, alarm=True, alarm_memory=True)
    assert out[3] == m.NativeAreaStatus(mode=AreaMode.TOTAL, alarm=False, alarm_memory=False)
    assert out[5] == m.NativeAreaStatus(mode=AreaMode.DISARMED, alarm=False, alarm_memory=True)


async def test_area_statuses_reuse_persistent_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    frames = [_status_resp({0: b"\x00\x04\x10"}), _status_resp({0: b"\x00\x01\x10"})]
    pairs = _patch_sessions(monkeypatch, [frames])
    client = m.Local6004Client("host", "pass")

    first = await client.async_get_area_statuses()
    second = await client.async_get_area_statuses()

    assert first[0].mode is AreaMode.DISARMED
    assert second[0].mode is AreaMode.TOTAL
    # one connection, two status commands, both flagged as command frames
    sent = pairs[0][1].sent
    assert len(sent) == 2
    assert all(frame[4:6] == b"\x01\x00" for frame in sent)
    await client.async_close()
    await client.async_close()  # idempotent


async def test_area_statuses_error_drops_connection_and_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = b"\xaa\xaa" + _status_resp({})[2:]
    _patch_sessions(monkeypatch, [[bad], [_status_resp({3: b"\x00\x01\x10"})]])
    client = m.Local6004Client("host", "pass")

    with pytest.raises(m.Local6004Error):
        await client.async_get_area_statuses()
    assert client._status_conn is None

    assert (await client.async_get_area_statuses())[3].mode is AreaMode.TOTAL


async def test_area_statuses_timeout_and_close_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _HangingReader(_FakeReader):
        async def readexactly(self, n: int) -> bytes:
            await asyncio.sleep(5)
            return b""

    class _RaisingWriter(_FakeWriter):
        async def wait_closed(self) -> None:
            raise OSError("already closed")

    async def fake_open(host: str, port: int):  # noqa: ANN202
        return _HangingReader(b""), _RaisingWriter()

    monkeypatch.setattr(m.asyncio, "open_connection", fake_open)
    client = m.Local6004Client("host", "pass")
    with pytest.raises(m.Local6004Error, match="TimeoutError"):
        await client.async_get_area_statuses(timeout=0.05)
    assert client._status_conn is None


def test_decode_partition_statuses_rejects_short_response() -> None:
    with pytest.raises(ValueError, match="short"):
        m.decode_partition_statuses(bytes(m._STATUS_HEADER + 10))


async def test_area_statuses_short_response_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sessions(monkeypatch, [[_resp(bytes(m._STATUS_HEADER))]])
    client = m.Local6004Client("host", "pass")
    with pytest.raises(m.Local6004Error, match="short"):
        await client.async_get_area_statuses()
    assert client._status_conn is None


async def test_close_does_not_hang_on_stalled_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    class _StalledWriter(_FakeWriter):
        async def wait_closed(self) -> None:
            await asyncio.sleep(10)

    async def fake_open(host: str, port: int):  # noqa: ANN202
        return _FakeReader(_status_resp({})), _StalledWriter()

    monkeypatch.setattr(m.asyncio, "open_connection", fake_open)
    monkeypatch.setattr(m, "_CLOSE_TIMEOUT", 0.05)
    client = m.Local6004Client("host", "pass")
    await client.async_get_area_statuses()
    await asyncio.wait_for(client.async_close(), 1)  # bounded, lock released
    assert client._status_conn is None


# --------------------------------------------------------------- terminal status
def _term(kind: int, a: bytes = b"\x00\x00\x00\x00", b: bytes = b"\x00\x00\x00\x00") -> bytes:
    return bytes([kind, 0]) + a + b


def _terminal_resp(records: list[bytes]) -> bytes:
    data = b"".join(records).ljust(m._TERMINAL_REC * m._TERMINAL_CHUNK, b"\x00")
    return _resp(bytes(m._STATUS_HEADER) + data)


def test_zone_terminal_mapping() -> None:
    assert m.zone_terminal(16) == (16, 0)
    assert m.zone_terminal(1021) == (16, 1)
    assert m.zone_terminal(1005) == (0, 1)


def test_terminal_chunks() -> None:
    assert m.terminal_chunks(set()) == []
    assert m.terminal_chunks({0, 1, 2, 3, 4, 10, 19}) == [(0, 20)]
    assert m.terminal_chunks({0, 19, 20, 45}) == [(0, 20), (20, 21), (45, 46)]


def test_terminal_status_cmd_guard() -> None:
    assert m._status_cmd(7, b"\x00\x00\x14\x00")[-4:] == b"\x00\x00\x14\x00"
    with pytest.raises(AssertionError):
        m._status_cmd(9)  # SET_ZONE_BYPASS must never be built


def test_decode_terminal_statuses() -> None:
    records = [
        _term(3, b"\x18\x00\x02\x00", b"\x08\x00\x01\x00"),  # t0 double: A open+excluded, B ready
        _term(0, b"\x09\x00\x01\x00"),  # t1 single: ready, alarm memory
        _term(1),  # t2 output -> skipped
        _term(4),  # t3 disabled -> skipped
        _term(0, b"\x08\x00\x07\x00"),  # t4 single with unknown state -> skipped
    ]
    resp = bytes(m._STATUS_HEADER) + b"".join(records)
    out = m.decode_terminal_statuses(resp, 0, 5)
    assert set(out) == {0, 1005, 1}
    assert out[0] == m.NativeZoneStatus(state=ZoneState.ALARM, excluded=True, alarm_memory=False)
    assert out[1005] == m.NativeZoneStatus(state=ZoneState.READY, excluded=False, alarm_memory=False)
    assert out[1] == m.NativeZoneStatus(state=ZoneState.READY, excluded=False, alarm_memory=True)
    with pytest.raises(ValueError, match="short"):
        m.decode_terminal_statuses(resp, 0, 20)


async def test_zone_statuses_chunks_and_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _terminal_resp([_term(3, b"\x08\x00\x01\x00", b"\x18\x00\x02\x00")])
    second = _terminal_resp([_term(0, b"\x08\x00\x02\x00")])
    pairs = _patch_sessions(monkeypatch, [[first, second]])
    client = m.Local6004Client("host", "pass")

    out = await client.async_get_zone_statuses({0, 1005, 30})

    assert out[0].state is ZoneState.READY
    assert out[1005].excluded
    assert out[30].state is ZoneState.ALARM
    sent = pairs[0][1].sent
    assert len(sent) == 2  # [0,1) then [30,31), one connection


async def test_zone_statuses_error_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sessions(monkeypatch, [[_resp(bytes(m._STATUS_HEADER))]])
    client = m.Local6004Client("host", "pass")
    with pytest.raises(m.Local6004Error, match="short"):
        await client.async_get_zone_statuses({0})
    assert client._status_conn is None
