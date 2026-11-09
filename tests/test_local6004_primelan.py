"""Unit tests for the PrimeLAN outer frame (fake streams only, never a panel)."""

import pytest

from custom_components.inim_prime.client import local6004 as m
from tests.test_local6004 import _FakeReader, _FakeWriter

KEY, IV = m.make_key_iv("pass")
_EMPTY_PARTITIONS = bytes(m._STATUS_HEADER + m._PARTITION_REC * m._PARTITION_COUNT)


def _inner(plain: bytes, preamble: bytes = b"\x50\x50") -> bytes:
    return preamble + m._build_frame(plain, KEY, IV, first=True)[2:]


def _decrypt_inner(frame: bytes) -> bytes:
    return m._unpad(m._aes(KEY, IV, frame[10:], decrypt=True))


@pytest.mark.parametrize("preamble", [b"\x50\x50", b"\x50\x54"])
async def test_status_read_through_primelan(
    monkeypatch: pytest.MonkeyPatch, preamble: bytes
) -> None:
    reader = _FakeReader(m._wrap_outer(_inner(_EMPTY_PARTITIONS, preamble), 0))
    writer = _FakeWriter()

    async def fake_open(host: str, port: int) -> tuple[_FakeReader, _FakeWriter]:
        return reader, writer

    monkeypatch.setattr(m.asyncio, "open_connection", fake_open)
    client = m.Local6004Client("host", "pass", use_outer_frame=True)

    assert await client.async_get_area_statuses() == {}

    (sent,) = writer.sent
    assert sent[:4] == b"\x50\x53\x00\x00"
    assert int.from_bytes(sent[4:8], "little") == len(sent) - 12
    assert sent[8:12] == bytes(4)  # commands and status reads: no length hint
    assert sent[12:14] == b"\x50\x50"
    assert _decrypt_inner(sent[12:]) == m._status_cmd(6)


async def test_memory_read_announces_its_answer_length() -> None:
    data = bytes(range(16))
    reader = _FakeReader(m._wrap_outer(_inner(data + b"\x00"), 0))
    writer = _FakeWriter()
    client = m.Local6004Client("host", "pass", use_outer_frame=True)

    assert await client._read(reader, writer, 0x14030D20, 16) == data

    (sent,) = writer.sent
    # 16 data + 1 checksum -> 32 bytes of ciphertext, + 10 header.
    assert int.from_bytes(sent[8:12], "little") == 42
    assert m._read_response_len(1024) == 1050


@pytest.mark.parametrize(
    ("answer", "match"),
    [
        (b"\x50\x50" + bytes(10), "bad outer header"),
        (b"\x50\x53\x01\x00" + bytes(8), "bad outer header"),
        (b"\x50\x53\x00\x00" + (4).to_bytes(4, "little") + bytes(4), "4-byte inner frame"),
        (m._wrap_outer(b"\xaa\xaa" + _inner(_EMPTY_PARTITIONS)[2:], 0), "bad preamble"),
    ],
)
async def test_bad_primelan_answers(answer: bytes, match: str) -> None:
    client = m.Local6004Client("host", "pass", use_outer_frame=True)
    with pytest.raises(ValueError, match=match):
        await client._xfer(_FakeReader(answer), _FakeWriter(), b"x", first=True)


async def test_inner_length_must_match_the_outer_header() -> None:
    inner = _inner(_EMPTY_PARTITIONS)
    lying = inner[:6] + (len(inner) + 16).to_bytes(4, "little") + inner[10:]
    client = m.Local6004Client("host", "pass", use_outer_frame=True)
    with pytest.raises(ValueError, match="length mismatch"):
        await client._xfer(
            _FakeReader(m._wrap_outer(lying, 0)), _FakeWriter(), b"x", first=True
        )