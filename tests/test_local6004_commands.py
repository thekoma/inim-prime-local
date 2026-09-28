"""Unit tests for the TCP 6004 write commands (fake streams only, never a panel).

The expected command bodies below were generated with Pitscheider's
inim-prime-native (``assemble_payload`` of each operation), so they pin this
client to the layouts documented there.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from custom_components.inim_prime.client import local6004 as m
from custom_components.inim_prime.client.const import AreaMode
from tests.test_local6004 import _decrypt, _FakeReader, _FakeWriter, _resp, _status_resp

# Constant tail of the 18-byte response header observed on a PrimeX 4.07.
_HEADER_TAIL = bytes.fromhex("0100ffffff030000ffffffffffff")


def _ack(op: int) -> bytes:
    """A command response frame echoing ``op`` the way the panel does."""
    return _resp((~op & 0xFFFFFFFF).to_bytes(4, "little") + _HEADER_TAIL)


# ------------------------------------------------------------ byte-exact bodies
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            m._write_cmd(3, m.arming_data({4: AreaMode.TOTAL})),
            "0300000074000000000000000000010000000000000000000000000000000000000000000000"
            "0000",
        ),
        (
            m._write_cmd(
                3,
                m.arming_data(
                    {0: AreaMode.PARTIAL, 2: AreaMode.DISARMED, 29: AreaMode.SNAPSHOT}
                ),
            ),
            "0300000074000000000002000400000000000000000000000000000000000000000000000000"
            "0003",
        ),
        (
            m._write_cmd(3, m.arming_data({4: AreaMode.DISARMED}), "1234"),
            "0300000001020304ffff00000000040000000000000000000000000000000000000000000000"
            "0000",
        ),
        (m._write_cmd(16, m.reset_data([4])), "1000000074000000000010000000"),
        (m._write_cmd(16, m.reset_data([0, 9, 29])), "1000000074000000000001020020"),
        (m._write_cmd(8, m.output_data(1007, True)), "08000000740000000000ef030100"),
        (m._write_cmd(8, m.output_data(1005, False)), "08000000740000000000ed030000"),
        (m._write_cmd(9, m.zone_bypass_data(12, True)), "090000007400000000000c000000"),
        (m._write_cmd(9, m.zone_bypass_data(1017, False)), "09000000740000000000f9030200"),
    ],
)
def test_write_bodies_match_pitscheider(body: bytes, expected: str) -> None:
    assert body.hex() == expected


def test_encode_pin() -> None:
    assert m.encode_pin(None) == b"\x74\x00\x00\x00\x00\x00"
    assert m.encode_pin("1234") == b"\x01\x02\x03\x04\xff\xff"
    assert m.encode_pin("090807") == b"\x00\x09\x00\x08\x00\x07"
    for bad in ("", "1234567", "12a4", "١٢"):  # last: Arabic-Indic digits
        with pytest.raises(ValueError, match="PIN"):
            m.encode_pin(bad)


@pytest.mark.parametrize(
    "build",
    [
        lambda: m.arming_data({}),
        lambda: m.arming_data({30: AreaMode.TOTAL}),
        lambda: m.arming_data({-1: AreaMode.TOTAL}),
        lambda: m.arming_data({0: 5}),  # type: ignore[dict-item]
        lambda: m.arming_data({0: 0}),  # type: ignore[dict-item]
        lambda: m.reset_data([]),
        lambda: m.reset_data([30]),
        lambda: m.zone_bypass_data(2010, True),
        lambda: m.zone_bypass_data(-1, True),
        lambda: m.output_data(1004, True),
        lambda: m.output_data(1010, True),
    ],
)
def test_data_builders_reject_bad_arguments(build) -> None:  # noqa: ANN001
    with pytest.raises(ValueError):
        build()


# -------------------------------------------------------------------- guards
@pytest.mark.parametrize("op", [0, 1, 2, 4, 5, 6, 7, 10, 11, 15, 17, 23, 0x11, 0x17])
def test_write_guard_rejects_other_opcodes(op: int) -> None:
    with pytest.raises(m.OpcodeNotAllowed):
        m._write_cmd(op, b"")


@pytest.mark.parametrize("op", [3, 8, 9, 10, 16])
def test_status_guard_rejects_write_opcodes(op: int) -> None:
    with pytest.raises(m.OpcodeNotAllowed):
        m._status_cmd(op)


def test_read_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "_READ_OPS", frozenset())
    with pytest.raises(m.OpcodeNotAllowed):
        m._read_cmd(0, 4, cont=False)


def test_allow_lists_are_disjoint_and_exact() -> None:
    assert frozenset({3, 8, 9, 16}) == m._WRITE_OPS
    assert frozenset({6, 7}) == m._STATUS_OPS
    assert frozenset({0x10, 0x11}) == m._READ_OPS
    # Memory-read opcodes are a byte inside a read body, a different namespace
    # from the 4-byte command opcode (0x10 read-continue vs op 16 reset), so
    # only the two command lists must not overlap.
    assert not m._WRITE_OPS & m._STATUS_OPS


def test_guards_hold_under_python_optimize() -> None:
    """``python -O`` strips asserts; the guards must still raise."""
    code = (
        "from custom_components.inim_prime.client import local6004 as m\n"
        "assert False, 'asserts are on'\n"
    )
    probe = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, check=False)
    assert probe.returncode == 0, "the interpreter did not strip asserts"
    code = (
        "from custom_components.inim_prime.client import local6004 as m\n"
        "for build in (lambda: m._write_cmd(6, b''), lambda: m._status_cmd(3),\n"
        "              lambda: m._write_cmd(0x11, b'')):\n"
        "    try:\n"
        "        build()\n"
        "    except m.OpcodeNotAllowed:\n"
        "        continue\n"
        "    raise SystemExit('guard bypassed')\n"
        "m._READ_OPS = frozenset()\n"
        "try:\n"
        "    m._read_cmd(0, 4, cont=False)\n"
        "except m.OpcodeNotAllowed:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit('read guard bypassed')\n"
    )
    result = subprocess.run(
        [sys.executable, "-O", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


# -------------------------------------------------------------- response check
def test_check_command_response() -> None:
    m.check_command_response(3, (~3 & 0xFFFFFFFF).to_bytes(4, "little") + _HEADER_TAIL)
    with pytest.raises(m.NativeCommandRejected, match="unexpected response header f9 ff"):
        m.check_command_response(3, (~6 & 0xFFFFFFFF).to_bytes(4, "little") + _HEADER_TAIL)
    with pytest.raises(m.NativeCommandRejected, match="empty"):
        m.check_command_response(9, b"")
    assert issubclass(m.NativeCommandRejected, m.NativeCommandUncertain)
    assert not issubclass(m.NativeCommandUncertain, m.NativeCommandNotSent)


def test_scene_target_modes() -> None:
    assert m.scene_target_modes({0: "away", 1: "stay", 4: "disarm"}) == {
        0: AreaMode.TOTAL,
        1: AreaMode.PARTIAL,
        4: AreaMode.DISARMED,
    }
    assert m.scene_target_modes({0: "away", 1: "away+stay"}) is None
    assert m.scene_target_modes({0: "0x8"}) is None
    assert m.scene_target_modes({}) is None


# ------------------------------------------------------------- async commands
class _Opener:
    """Fake ``asyncio.open_connection`` serving scripted streams, counting calls."""

    def __init__(self, *pairs: tuple[_FakeReader, _FakeWriter] | BaseException) -> None:
        self._pairs = list(pairs)
        self.calls = 0

    async def __call__(self, host: str, port: int) -> tuple[_FakeReader, _FakeWriter]:
        self.calls += 1
        item = self._pairs.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _stream(*frames: bytes) -> tuple[_FakeReader, _FakeWriter]:
    return _FakeReader(b"".join(frames)), _FakeWriter()


def _install(monkeypatch: pytest.MonkeyPatch, opener: _Opener) -> None:
    monkeypatch.setattr(m.asyncio, "open_connection", opener)


@pytest.mark.parametrize(
    ("call", "op", "data"),
    [
        (
            lambda c: c.async_set_area_modes({4: AreaMode.TOTAL, 5: AreaMode.DISARMED}),
            3,
            bytes(4) + b"\x01\x04" + bytes(24),
        ),
        (lambda c: c.async_set_zone_bypass(1017, True), 9, bytes.fromhex("f9030000")),
        (lambda c: c.async_set_output(1009, False), 8, bytes.fromhex("f1030000")),
        (lambda c: c.async_reset_areas({1, 3}), 16, bytes.fromhex("0a000000")),
    ],
)
async def test_command_sends_one_exact_frame(
    monkeypatch: pytest.MonkeyPatch, call, op: int, data: bytes  # noqa: ANN001
) -> None:
    stream = _stream(_ack(op))
    _install(monkeypatch, _Opener(stream))
    client = m.Local6004Client("host", "pass")

    await call(client)

    (frame,) = stream[1].sent
    assert frame[4:6] == b"\x01\x00"  # command frame flag
    assert _decrypt(frame) == op.to_bytes(4, "little") + m._NO_PIN + data
    assert client._status_conn is stream  # kept for the status reads


async def test_command_with_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _stream(_ack(16))
    _install(monkeypatch, _Opener(stream))
    await m.Local6004Client("host", "pass").async_reset_areas([0], pin="42")
    assert _decrypt(stream[1].sent[0])[4:10] == b"\x04\x02\xff\xff\xff\xff"


async def test_command_probes_a_kept_connection_first(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _stream(_status_resp({}), _status_resp({}), _ack(9))
    _install(monkeypatch, _Opener(stream))
    client = m.Local6004Client("host", "pass")
    await client.async_get_area_statuses()

    await client.async_set_zone_bypass(3, False)

    status, probe, command = (_decrypt(f) for f in stream[1].sent)
    assert status == probe == m._status_cmd(6)
    assert command[:4] == b"\x09\x00\x00\x00"


async def test_command_reconnects_when_the_probe_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = _stream(_status_resp({}))  # answers the first read, then EOF
    fresh = _stream(_ack(3))
    _install(monkeypatch, _Opener(stale, fresh))
    client = m.Local6004Client("host", "pass")
    await client.async_get_area_statuses()

    await client.async_set_area_modes({0: AreaMode.DISARMED})

    # Only the read-only probe went to the stale connection.
    assert [_decrypt(f)[:4] for f in stale[1].sent] == [b"\x06\x00\x00\x00"] * 2
    assert [_decrypt(f)[:4] for f in fresh[1].sent] == [b"\x03\x00\x00\x00"]
    assert client._status_conn is fresh


async def test_command_replaces_a_stalled_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    class _StallsAfterFirst(_FakeReader):
        async def readexactly(self, n: int) -> bytes:
            if self._pos >= len(self._buf):
                await asyncio.sleep(5)
            return await super().readexactly(n)

    stalled = (_StallsAfterFirst(_status_resp({})), _FakeWriter())
    fresh = _stream(_ack(8))
    _install(monkeypatch, _Opener(stalled, fresh))
    monkeypatch.setattr(m, "STATUS_TIMEOUT", 0.05)
    client = m.Local6004Client("host", "pass")
    await client.async_get_area_statuses()

    await client.async_set_output(1005, True, timeout=2)

    assert [_decrypt(f)[:4] for f in stalled[1].sent] == [b"\x06\x00\x00\x00"] * 2
    assert [_decrypt(f)[:4] for f in fresh[1].sent] == [b"\x08\x00\x00\x00"]


async def test_connect_failure_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _Opener(OSError("refused"))
    _install(monkeypatch, opener)
    client = m.Local6004Client("host", "pass")
    with pytest.raises(m.NativeCommandNotSent, match="native command 3: refused"):
        await client.async_set_area_modes({0: AreaMode.TOTAL})
    assert opener.calls == 1
    assert client._status_conn is None


async def test_connect_timeout_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hang(host: str, port: int) -> None:
        await asyncio.sleep(5)

    monkeypatch.setattr(m.asyncio, "open_connection", hang)
    with pytest.raises(m.NativeCommandNotSent, match="TimeoutError"):
        await m.Local6004Client("host", "pass").async_set_output(1005, True, timeout=0.05)


async def test_lost_answer_is_uncertain(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _stream()  # accepts the write, then EOF
    _install(monkeypatch, _Opener(stream))
    client = m.Local6004Client("host", "pass")
    with pytest.raises(m.NativeCommandUncertain) as info:
        await client.async_set_zone_bypass(0, True)
    assert not isinstance(info.value, m.NativeCommandNotSent)
    assert len(stream[1].sent) == 1  # sent once, never retried
    assert client._status_conn is None


async def test_timeout_after_sending_is_uncertain(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Hanging(_FakeReader):
        async def readexactly(self, n: int) -> bytes:
            await asyncio.sleep(5)
            return b""

    writer = _FakeWriter()
    _install(monkeypatch, _Opener((_Hanging(b""), writer)))
    with pytest.raises(m.NativeCommandUncertain, match="TimeoutError"):
        await m.Local6004Client("host", "pass").async_reset_areas([2], timeout=0.05)
    assert len(writer.sent) == 1


async def test_unexpected_answer_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _stream(_ack(6))
    _install(monkeypatch, _Opener(stream))
    client = m.Local6004Client("host", "pass")
    with pytest.raises(m.NativeCommandRejected):
        await client.async_set_output(1006, True)
    # The exchange completed, so the stream is still aligned and kept.
    assert client._status_conn is stream


async def test_cancelled_command_drops_the_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Hanging(_FakeReader):
        async def readexactly(self, n: int) -> bytes:
            await asyncio.sleep(5)
            return b""

    _install(monkeypatch, _Opener((_Hanging(b""), _FakeWriter())))
    client = m.Local6004Client("host", "pass")
    task = asyncio.ensure_future(client.async_set_zone_bypass(1, True))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client._status_conn is None
    client._drop_status_conn()  # idempotent


async def test_bad_arguments_never_open_a_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _Opener()
    _install(monkeypatch, opener)
    client = m.Local6004Client("host", "pass")
    with pytest.raises(ValueError):
        await client.async_set_output(1, True)
    with pytest.raises(ValueError, match="PIN"):
        await client.async_reset_areas([0], pin="x")
    assert opener.calls == 0
