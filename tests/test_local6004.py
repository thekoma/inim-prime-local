"""Unit tests for the TCP 6004 client's reads (no Home Assistant, no socket)."""

from __future__ import annotations

import asyncio
from dataclasses import replace

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


def test_read_cmd_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-read memory opcode is refused even if the constants were changed."""
    monkeypatch.setattr(m, "_READ_CONT", 0x12)
    with pytest.raises(m.ReadOnlyViolation):
        m._read_cmd(0x10, 4, cont=True)


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
def _decrypt(frame: bytes) -> bytes:
    """Recover the application payload a client frame carries."""
    return m._unpad(m._aes(_KEY, _IV, frame[10:], decrypt=True))


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


def _label(text: str) -> bytes:
    return text.encode("latin-1").ljust(m._LABEL_REC, b" ")


def _term(kind: int, a: bytes = b"\x00\x00\x00\x00", b: bytes = b"\x00\x00\x00\x00") -> bytes:
    return bytes([kind, 0]) + a + b


def _partition_frames() -> list[bytes]:
    """Script the partition scan: partitions 0 and 2 configured."""
    part = bytearray(m._PARTITION_REC * m._PARTITION_COUNT)
    part[2] = part[8] = m._PARTITION_CONFIGURED
    return [_resp(bytes(m._STATUS_HEADER) + bytes(part))]


def _terminal_frames(terminals: dict[int, int]) -> list[bytes]:
    """Script the terminal scan (unlisted terminals are disabled)."""
    frames = []
    for start in range(0, m._TERMINAL_COUNT, m._TERMINAL_CHUNK):
        end = min(start + m._TERMINAL_CHUNK, m._TERMINAL_COUNT)
        records = [_term(terminals.get(t, 4)) for t in range(start, end)]
        frames.append(_resp(bytes(m._STATUS_HEADER) + b"".join(records)))
    return frames


def _label_frames(
    zones: dict[int, tuple[str, int]],
    spans: list[tuple[int, int]],
    kinds: bool = True,
) -> list[bytes]:
    """Script the label session: ack, labels, then labels + settings per span."""
    areas = b"".join(
        _label(n) for n in ["Home", "AREA       002", "Garage"] + ["AREA"] * 27
    )
    scenarios = b"".join(
        [_label("Away"), _label("Scenario 2"), _label("Scenario 9"), _label("")]
        + [_label(f"SCENARIO   {i + 1:03d}") for i in range(4, m._SCENARIO_COUNT)]
    )
    outputs = b"".join(_label(n) for n in ("Siren", "Buzzer", "Box", "AUX 1", "AUX 2"))
    frames = [_resp(b"\x00\x00\x00\x00"), _resp(areas), _resp(scenarios), _resp(outputs)]
    for first, last in spans:
        labels = bytearray()
        settings = bytearray()
        for zid in range(first, last + 1):
            label, mask = zones.get(zid, ("", 0))
            labels += label.encode("latin-1").ljust(16, b"\x00")
            zone_type, options, options1, pulses = _ZONE_EXTRA.get(zid, (0, 0, 0, 1))
            settings += (
                mask.to_bytes(4, "little")
                + bytes([zone_type, options, options1])
                + bytes(3)
                + bytes([pulses])
            )
        frames += [_resp(bytes(labels)), _resp(bytes(settings))]
    if spans and kinds:
        # Zone-kind reads: the third option byte per span, then the settings and
        # detector types of the zones' terminals.
        frames += [
            _resp(bytes(_ZONE_OPT2.get(z, 0) for z in range(first, last + 1)))
            for first, last in spans
        ]
        terminals = range(
            min(first % 1005 for first, _ in spans), max(last % 1005 for _, last in spans) + 1
        )
        frames.append(_resp(b"".join(_TERMINAL_CFG.get(t, bytes(12)) for t in terminals)))
        frames.append(_resp(b"".join(_PULSE_CFG.get(t, bytes(28)) for t in terminals)))
    return frames


_ACK = _resp(b"\x00\x00\x00\x00")
# t0 double, t1 single, t2 single but in no partition, t3 unknown type,
# t5 double; outputs 1005 and 1007 (1006 disabled).
_TERMINALS = {0: 3, 1: 0, 2: 0, 3: 2, 5: 3, 1005: 1, 1007: 1}
_ZONES = {
    0: ("Door", 0x01),
    1: ("Window", 0x06),
    2: ("Unused", 0),
    5: ("Hall", 1 << 29),
    1005: ("Door B", 0x01),
    1010: ("Hall B", 0x04),
}
_SPANS = [(0, 5), (1005, 1010)]
# Zone settings bytes 4, 5, 6 and 10 (type, options, options1, pulses).
_ZONE_EXTRA = {1: (0, 0x02, 0, 1), 5: (0, 0x01, 0x40, 1), 1005: (1, 0, 0, 1), 1010: (0, 0, 0, 5)}
# The third option byte per zone (0x04 = fire).
_ZONE_OPT2 = {1: 0x04}
# Detector type (byte 10): terminal 5 is a roller shutter.
_PULSE_CFG = {5: bytes(10) + bytes([1]) + bytes(17)}
# Terminal settings: thresholds, balance (byte 5), levels (6-8), type (11).
_TERMINAL_CFG = {
    0: bytes([16, 56, 91, 125, 180, 4, 0, 0, 0, 0, 0, 3]),
    5: bytes([16, 56, 91, 125, 180, 1, 0x92, 0x12, 0x12, 0, 0, 3]),
}


def _config_sessions() -> list[list[bytes]]:
    return [[_ACK, _version_frame("4.07 PX020")], [_ACK, _modi_frame()]]


async def test_async_read_config_success(monkeypatch: pytest.MonkeyPatch) -> None:
    pairs = _patch_sessions(
        monkeypatch,
        [
            *_config_sessions(),
            _partition_frames(),
            _terminal_frames(_TERMINALS),
            _label_frames(_ZONES, _SPANS),
        ],
    )
    cfg = await m.Local6004Client("host", "pass").async_read_config()

    # Byte-exact frames: the scans send only read-only status commands with no
    # context op, flagged as command frames; the label session opens with the
    # config context and then only reads.
    part_sent, term_sent, label_sent = (pairs[i][1].sent for i in (2, 3, 4))
    assert part_sent == [m._build_frame(b"\x06\x00\x00\x00" + m._NO_PIN, _KEY, _IV, first=True)]
    chunks = [(s, min(s + 20, 1010)) for s in range(0, 1010, 20)]
    assert term_sent == [
        m._build_frame(
            b"\x07\x00\x00\x00" + m._NO_PIN + s.to_bytes(2, "little") + e.to_bytes(2, "little"),
            _KEY,
            _IV,
            first=True,
        )
        for s, e in chunks
    ]
    reads = [
        (0x14030D20, 480),
        (0x1403D8E0, 800),
        (0x1403DEB0, 80),
        (0x14030F00, 6 * 16),
        (0x14073414, 6 * 11),
        (0x14030F00 + 1005 * 16, 6 * 16),
        (0x14073414 + 1005 * 11, 6 * 11),
        (0x1406F8A0, 6),
        (0x1406F8A0 + 1005, 6),
        (0x140704F8, 6 * 12),
        (0x14100A10, 6 * 28),
    ]
    assert [(_decrypt(f), f[4:6]) for f in label_sent] == [(m._OPEN_CFG, b"\x01\x00")] + [
        (m._read_cmd(addr, n, cont=False), b"\x00\x00") for addr, n in reads
    ]

    assert cfg.layout_ok
    assert cfg.firmware == "4.07 PX020"
    assert [s.id for s in cfg.scenes] == [0]
    assert cfg.scenes[0].arms == {0: "away", 1: "away", 2: "away"}
    assert cfg.zone_areas == {0: [0], 1005: [0], 1: [1, 2], 5: [29], 1010: [2]}
    st = cfg.structure
    assert st is not None
    assert st.areas == [m.NativeObject(0, "Home"), m.NativeObject(2, "Garage")]
    # terminal order, half A before half B; zone 2 (no partition) is dropped
    door_terminal = {"balance": 4, "levels": (0, 0, 0), "terminal_type": 3}
    hall_terminal = {"balance": 1, "levels": (0x92, 0x12, 0x12), "terminal_type": 3}
    plain_terminal = {"balance": 0, "levels": (0, 0, 0), "terminal_type": 0}
    def zone(zid, label, terminal, areas, zone_type, options, options1, pulses, opt2, term, sensor):  # noqa: ANN001, ANN202, PLR0913
        return m.NativeZoneDef(
            zid, label, terminal, areas, zone_type=zone_type, options=options, options1=options1,
            pulses=pulses, options2=opt2, sensor=sensor, **term,
        )

    assert st.zones == [
        zone(0, "Door", 0, (0,), 0, 0, 0, 1, 0, door_terminal, 0),
        zone(1005, "Door B", 0, (0,), 1, 0, 0, 1, 0, door_terminal, 0),
        zone(1, "Window", 1, (1, 2), 0, 0x02, 0, 1, 0x04, plain_terminal, 0),
        zone(5, "Hall", 5, (29,), 0, 0x01, 0x40, 1, 0, hall_terminal, 1),
        zone(1010, "Hall B", 5, (2,), 0, 0, 0, 5, 0, hall_terminal, 1),
    ]
    # The named zone kinds (Prime/STUDIO names) read from those bytes.
    door_b, window, hall = st.zones[1], st.zones[2], st.zones[3]
    assert door_b.is_delayed and not door_b.is_24h
    assert window.is_fire and not window.is_interior
    assert hall.is_shutter and hall.is_interior and hall.is_fault_zone
    assert not hall.is_shock and not hall.is_hold_up and not hall.has_chime
    assert window.bypassable and m.NativeZoneDef(9, "X", 9, (0,)).bypassable
    assert not replace(window, options=0x04).bypassable
    # "Scenario 2" is scenario 1's default; "Scenario 9" on scenario 2 is not
    assert st.scenarios == [m.NativeObject(0, "Away"), m.NativeObject(2, "Scenario 9")]
    assert st.outputs == [m.NativeObject(1005, "Siren"), m.NativeObject(1007, "Box")]


async def test_async_read_config_without_zones(monkeypatch: pytest.MonkeyPatch) -> None:
    """A panel with no zone terminals reads no zone spans."""

    class _RaisingWriter(_FakeWriter):
        async def wait_closed(self) -> None:
            raise OSError("already closed")

    streams = iter(
        [
            (_FakeReader(b"".join(frames)), _FakeWriter())
            for frames in _config_sessions()
        ]
        + [
            (_FakeReader(b"".join(_partition_frames())), _RaisingWriter()),
            (_FakeReader(b"".join(_terminal_frames({}))), _FakeWriter()),
            (_FakeReader(b"".join(_label_frames({}, []))), _FakeWriter()),
        ]
    )

    async def fake_open(host: str, port: int):  # noqa: ANN202
        return next(streams)

    monkeypatch.setattr(m.asyncio, "open_connection", fake_open)
    cfg = await m.Local6004Client("host", "pass").async_read_config()
    assert cfg.structure is not None
    assert cfg.structure.zones == []
    assert cfg.structure.outputs == []
    assert cfg.zone_areas == {}


def test_short_zone_settings_drop_the_zone() -> None:
    """Settings too short for a zone read as an empty mask: the zone is not in use.

    ``_read`` keeps reading until the requested length arrives, so this only
    guards the decoder against a truncated blob.
    """
    labels = b"Door".ljust(16, b"\x00") + b"Window".ljust(16, b"\x00")
    settings = (1).to_bytes(4, "little") + bytes(7)  # zone 1's record missing
    zones = m._decode_zones([0, 1], [(0, 1)], [labels, settings])
    assert zones == [
        m.NativeZoneDef(0, "Door", 0, (0,), zone_type=0, options=0, options1=0, pulses=0)
    ]


def test_partial_settings_and_kind_records_leave_the_kind_unknown() -> None:
    """A truncated zone record or kind read keeps the areas but no raw kind fields."""
    labels = b"Door".ljust(16, b"\x00")
    settings = (1).to_bytes(4, "little") + bytes(2)  # mask only, record cut short
    kinds = {"terminal": (0, bytes(5)), "pulse": (0, bytes(3)), "opt2_0": (0, b"")}
    zones = m._decode_zones([0], [(0, 0)], [labels, settings], 1005, kinds)
    assert zones == [m.NativeZoneDef(0, "Door", 0, (0,))]


async def test_layout_without_kind_tables_reads_no_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A layout with the zone-kind offsets unmapped still reads the zones."""
    bare = replace(m._LAYOUT_40X, terminal_cfg=None, pulse_cfg=None, zone_opt2=None)
    monkeypatch.setitem(m._LAYOUTS, "4", bare)
    _patch_sessions(
        monkeypatch,
        [
            *_config_sessions(),
            _partition_frames(),
            _terminal_frames(_TERMINALS),
            _label_frames(_ZONES, _SPANS, kinds=False),
        ],
    )
    cfg = await m.Local6004Client("host", "pass").async_read_config()
    assert cfg.structure is not None and cfg.structure.zones is not None
    door = cfg.structure.zones[0]
    assert (door.id, door.zone_type, door.balance, door.sensor, door.options2) == (0, 0, None, None, None)


_SHORT = _resp(bytes(m._STATUS_HEADER))


@pytest.mark.parametrize(
    ("sessions", "missing"),
    [
        # partition scan rejected -> areas unknown, the rest is native
        ([[_SHORT], _terminal_frames(_TERMINALS), _label_frames(_ZONES, _SPANS)], {"areas"}),
        # a terminal chunk short -> zones and outputs unknown (no zone spans read)
        (
            [_partition_frames(), [*_terminal_frames(_TERMINALS)[:3], _SHORT], _label_frames({}, [])],
            {"zones", "outputs"},
        ),
        # connection dropped mid-scan
        ([_partition_frames(), [b""], _label_frames({}, [])], {"zones", "outputs"}),
        # the label session fails -> nothing is known
        (
            [_partition_frames(), _terminal_frames(_TERMINALS), [_ACK]],
            {"areas", "zones", "scenarios", "outputs"},
        ),
    ],
)
async def test_structure_step_failure_only_drops_its_kinds(
    monkeypatch: pytest.MonkeyPatch, sessions: list[list[bytes]], missing: set[str]
) -> None:
    """Each structure step is best effort: the config still loads."""
    _patch_sessions(monkeypatch, [*_config_sessions(), *sessions])
    cfg = await m.Local6004Client("host", "pass").async_read_config()
    assert cfg.layout_ok
    assert [s.id for s in cfg.scenes] == [0]
    st = cfg.structure
    assert st is not None
    kinds = {"areas", "zones", "scenarios", "outputs"}
    assert {k for k in kinds if getattr(st, k) is None} == missing


async def test_structure_step_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A structure step that hangs is cut off by its own budget."""
    client = m.Local6004Client("host", "pass")
    monkeypatch.setattr(m, "STRUCTURE_TIMEOUT", 0.01)

    async def hang() -> int:
        await asyncio.sleep(5)
        return 1

    assert await client._best_effort(hang()) is None


async def test_async_read_config_truncated_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection closed mid-frame is a Local6004Error, not a crash."""
    _patch_sessions(monkeypatch, [[b"\x50\x50"]])
    with pytest.raises(m.Local6004Error, match="expected bytes"):
        await m.Local6004Client("host", "pass").async_read_config()


def test_decode_label() -> None:
    assert m.decode_label(b"AREA       006  ") == "AREA       006"
    assert m.decode_label(b"Box\x00\x00garbage\x00\x00\x00\x00\x00") == "Box"
    assert m.decode_label(bytes(16)) == ""
    assert m.decode_label(b"\xff" * 16) == ""  # erased EEPROM


@pytest.mark.parametrize(
    ("scenario_id", "label", "default"),
    [
        (9, "Scenario 10", True),
        (30, "SCENARIO   031", True),
        (0, "", True),
        (2, "Scenario 7", False),  # a user name that happens to look default
        (0, "Ins.Totale", False),
        (4, "scenario 5", False),
    ],
)
def test_is_factory_default_scenario(scenario_id: int, label: str, default: bool) -> None:
    assert m.is_factory_default_scenario(scenario_id, label) is default


def test_structure_decoders_reject_short_responses() -> None:
    with pytest.raises(ValueError, match="short"):
        m.decode_terminal_types(bytes(m._STATUS_HEADER), 0, 20)
    with pytest.raises(ValueError, match="short"):
        m.decode_configured_partitions(bytes(m._STATUS_HEADER + 3))


def test_zone_candidates() -> None:
    types = {0: 3, 1: 0, 2: 1, 3: 4, 4: 2, 1005: 1}
    assert m.zone_candidates(types) == [0, 1005, 1]


async def test_async_read_config_wrong_firmware(monkeypatch: pytest.MonkeyPatch) -> None:
    ack = _resp(b"\x00\x00\x00\x00")
    _patch_sessions(monkeypatch, [[ack, _version_frame("3.10 PRIME")]])
    cfg = await m.Local6004Client("host", "pass").async_read_config()
    assert not cfg.layout_ok
    assert cfg.firmware == "3.10 PRIME"
    assert cfg.scenes == []


async def test_async_read_config_accepts_pt_preamble(monkeypatch: pytest.MonkeyPatch) -> None:
    """Firmware 5.x answers with the ``50 54`` preamble: it must be accepted.

    An unmapped major (6.x here) keeps the read to a single minimal session while
    still proving the preamble is accepted and an unknown layout degrades cleanly.
    """

    def pt(frame: bytes) -> bytes:
        return b"\x50\x54" + frame[2:]

    ack = pt(_resp(b"\x00\x00\x00\x00"))
    _patch_sessions(monkeypatch, [[ack, pt(_version_frame("6.00 PX020"))]])
    cfg = await m.Local6004Client("host", "pass").async_read_config()
    assert cfg.firmware == "6.00 PX020"
    assert not cfg.layout_ok
    assert cfg.scenes == []
    assert cfg.structure is None


def test_layout_50x_offsets() -> None:
    """Firmware 5.00 maps to the 50x layout with the +500-shifted offsets."""
    layout = m._LAYOUTS["5"]
    assert layout.half_offset == 1505
    assert layout.output_terminals == range(1505, 1510)
    assert layout.scenario_modi == 0x14099368
    assert layout.scenario_labels == 0x14041760
    assert layout.output_labels == 0x14041D30
    assert layout.area_labels == m._LAYOUT_40X.area_labels  # unchanged
    assert layout.zone_labels == m._LAYOUT_40X.zone_labels  # unchanged
    assert layout.zone_cfg == 0x1408B920  # recovered from Centrale5.dll, verified live
    assert layout.log_addr == 0x104A8  # spring_eep_logger_ev, verified live
    assert layout.native_writes_ok is True  # 5.00 native writes verified live


def test_supports_native_writes_per_layout() -> None:
    """Native writes are verified on 4.x but not on 5.00; the client reflects it."""
    assert m._LAYOUT_40X.native_writes_ok is True
    assert m._LAYOUT_50X.native_writes_ok is True
    client = m.Local6004Client("host", "pass")  # defaults to the 40x layout
    assert client.supports_native_writes is True


def test_zone_id_math_honors_50x_half_offset() -> None:
    """Zone id <-> terminal math and status decode use the layout half-offset."""
    assert m.zone_terminal(1505, 1505) == (0, 1)
    assert m.zone_terminal(1522, 1505) == (17, 1)
    assert m.zone_terminal(19, 1505) == (19, 0)
    assert m.zone_candidates({0: m._TERMINAL_DOUBLE}, 1505) == [0, 1505]
    # A double terminal 0 yields zone ids 0 (half A) and 1505 (half B) on 5.x.
    rec = bytes([m._TERMINAL_DOUBLE, 0, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x00])
    resp = b"\x00" * m._STATUS_HEADER + rec
    assert set(m.decode_terminal_statuses(resp, 0, 1, 1505)) == {0, 1505}


def test_output_data_honors_layout_output_range() -> None:
    """On 5.x outputs live at terminals 1505..1509; the 4.x ids are then rejected."""
    assert m.output_data(1505, True, range(1505, 1510)) == b"\xe1\x05\x01\x00"
    with pytest.raises(ValueError):
        m.output_data(1005, True, range(1505, 1510))
    assert m.output_data(1005, True)  # default range is still the 4.x one


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


def _class_table(*entries: tuple[int, int]) -> bytes:
    """Build an event-class table: (first code, kind) entries, then padding."""
    out = b"".join(start.to_bytes(2, "little") + bytes([0, kind, 0]) for start, kind in entries)
    return out + entries[-1][0].to_bytes(2, "little") + bytes(3) + bytes(10)


# A synthetic class table: class i starts at code 10 * i, with kind 1 (zone) for
# class 0, 2 (area) for class 2, 4 (key) for class 22, 13 (scenario) for class
# 30, an unknown kind for class 197, a one-code zone class at 1980, and the end
# sentinel at 2000.
_KINDS = {0: 1, 2: 2, 22: 4, 30: 13, 197: 99, 198: 1}
_CLASSES = m.decode_event_classes(
    _class_table(
        *[(10 * i, _KINDS.get(i, 0)) for i in range(199)], (1981, 0), (2000, 0)
    )
)


def _code_record(code: int, ts: int = 0x31000000, partmask: int = 0, flags: int = 0x80) -> bytes:
    return _log_record(ts, partmask, code & 0xFF, code >> 8, flags)


def test_decode_event_classes_stops_at_the_sentinel() -> None:
    assert len(_CLASSES) == 201  # 200 classes + the end sentinel
    assert _CLASSES[30] == m.EventClass(start=300, kind=13)
    assert [c.start for c in _CLASSES][-3:] == [1980, 1981, 2000]


def test_logger_class_table() -> None:
    reply = (~0x1F & 0xFFFFFFFF).to_bytes(4, "little") + (0x1A037C3C).to_bytes(4, "little")
    assert m.logger_class_table(reply + (735).to_bytes(4, "little")) == (0x1A037C3C, 735)
    assert m.logger_class_table(reply) is None  # too short
    assert m.logger_class_table(reply + (733).to_bytes(4, "little")) is None  # not 5-byte entries
    assert m.logger_class_table(reply + (0).to_bytes(4, "little")) is None
    assert m.logger_class_table(reply + (5 * 513).to_bytes(4, "little")) is None
    no_addr = reply[:4] + bytes(4) + (735).to_bytes(4, "little")
    assert m.logger_class_table(no_addr) is None


def test_decode_event_log() -> None:
    blob = (
        _code_record(3, partmask=0x01)  # class 0, zone alarm: zone 3
        + _code_record(21, flags=0x00)  # class 2, partition alarm: area 1, restoral
        + _code_record(225)  # class 22, valid key: key index 5
        + _code_record(303)  # class 30, scenario 3
        + _code_record(15)  # class 1: no object kind
        + _code_record(1975)  # class 197: unknown object kind
        + _code_record(1980)  # class 198: a one-code class names no object
        + _code_record(1995)  # class 199: beyond the named types
        + _code_record(2005, partmask=0x04)  # past the end sentinel: raw
        + _log_record(0, 0, 0, 0, 0)  # empty slot -> skipped
    )
    events = m.decode_event_log(
        blob, {0: "Home", 1: "Garage"}, {3: "Dis.Box"}, _CLASSES, {3: "Front door"}
    )
    assert len(events) == 9  # the empty slot is dropped
    assert events[0] == {
        "time": events[0]["time"],
        "event": "Zone alarm",
        "partitions": ["Home"],
        "restoral": False,  # 0x80 = set
        "zone": "Front door",
    }
    assert (events[1]["event"], events[1]["area"], events[1]["restoral"]) == (
        "Partition alarm",
        "Garage",
        True,  # flag clear
    )
    assert (events[2]["event"], events[2]["key"]) == ("Valid key", 6)  # 1-based number
    assert (events[3]["event"], events[3]["scenario"]) == ("Scenario", "Dis.Box")
    plain = ("time", "event", "partitions", "restoral")
    for event in events[4:7]:
        assert set(event) == set(plain)
    assert events[4]["event"] == "Terminal tamper"
    assert events[7]["event"] == "Event type 199"
    # a code past the last class renders raw; unlabeled partition falls back to areaN
    assert events[8]["event"] == "code:d507"
    assert events[8]["partitions"] == ["area3"]


def test_decode_event_log_without_classes_is_raw() -> None:
    events = m.decode_event_log(_code_record(303))
    assert events[0]["event"] == "code:2f01"
    assert "scenario" not in events[0]


def test_decode_event_log_unlabelled_zone_and_area() -> None:
    events = m.decode_event_log(_code_record(3) + _code_record(21), classes=_CLASSES)
    assert (events[0]["zone"], events[1]["area"]) == ("4", "2")  # 1-based fallbacks


class _LogConn:
    """A fake log session: the op 0x1F reply, then memory reads by address."""

    def __init__(self, rules: bytes, memory: dict[int, bytes]) -> None:
        self.rules = rules
        self.memory = memory
        self.reads: list[tuple[int, int]] = []


def _patch_log(monkeypatch: pytest.MonkeyPatch, client: m.Local6004Client, conn: _LogConn) -> None:
    writer = _FakeWriter()

    async def fake_open(host: str, port: int):  # noqa: ANN202
        return _FakeReader(b""), writer

    async def fake_xfer(reader, writer, app, *, first):  # noqa: ANN001, ANN202
        assert app == m._OPEN_LOG
        return conn.rules

    async def fake_read(reader, writer, addr, length):  # noqa: ANN001, ANN202
        conn.reads.append((addr, length))
        return conn.memory[addr][:length]

    monkeypatch.setattr(m.asyncio, "open_connection", fake_open)
    monkeypatch.setattr(client, "_xfer", fake_xfer)
    monkeypatch.setattr(client, "_read", fake_read)


def _rules(addr: int, length: int) -> bytes:
    return (~0x1F & 0xFFFFFFFF).to_bytes(4, "little") + addr.to_bytes(4, "little") + (
        length.to_bytes(4, "little") + bytes(16)
    )


async def test_async_read_event_log(monkeypatch: pytest.MonkeyPatch) -> None:
    table = _class_table(*[(10 * i, _KINDS.get(i, 0)) for i in range(199)], (1981, 0), (2000, 0))
    blob = _code_record(225, partmask=0x01)
    client = m.Local6004Client("host", "pass")
    conn = _LogConn(_rules(0x1A000000, 5 * 201), {0x1A000000: table, m._LOG_ADDR: blob})
    _patch_log(monkeypatch, client, conn)

    events = await client.async_read_event_log({0: "Home"})

    assert conn.reads == [(0x1A000000, 5 * 201), (m._LOG_ADDR, m._LOG_LEN)]
    assert (events[0]["event"], events[0]["key"], events[0]["partitions"]) == (
        "Valid key",
        6,
        ["Home"],
    )


async def test_async_read_event_log_without_a_class_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unusable op 0x1F reply still returns the events, with raw codes."""
    client = m.Local6004Client("host", "pass")
    conn = _LogConn(b"\xe0\xff\xff\xff", {m._LOG_ADDR: _code_record(225)})
    _patch_log(monkeypatch, client, conn)

    events = await client.async_read_event_log()

    assert conn.reads == [(m._LOG_ADDR, m._LOG_LEN)]
    assert events[0]["event"] == "code:e100"


async def test_event_log_reads_the_50x_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    client = m.Local6004Client("host", "pass")
    client._layout = m._LAYOUT_50X
    conn = _LogConn(b"", {0x104A8: b""})
    _patch_log(monkeypatch, client, conn)
    assert await client.async_read_event_log() == []
    assert conn.reads == [(0x104A8, m._LOG_LEN)]


async def test_event_log_unavailable_without_a_log_offset() -> None:
    """A layout with no mapped event-log offset errors instead of reading garbage."""
    client = m.Local6004Client("host", "pass")
    client._layout = replace(m._LAYOUT_50X, log_addr=None)
    with pytest.raises(m.Local6004Error, match="event log offset not mapped"):
        await client.async_read_event_log()


async def test_async_read_event_log_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = m.Local6004Client("host", "pass")

    async def boom(host: str, port: int):  # noqa: ANN202
        raise OSError("down")

    monkeypatch.setattr(m.asyncio, "open_connection", boom)
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
    with pytest.raises(m.ReadOnlyViolation):
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
    with pytest.raises(m.ReadOnlyViolation):
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


def test_decode_partition_statuses_sabotage() -> None:
    header = bytes(m._STATUS_HEADER)
    data = bytearray(m._PARTITION_REC * m._PARTITION_COUNT)
    data[0:3] = b"\x02\x04\x12"  # area 0: disarmed, sabotage (state 2), sabotage memory
    data[3:6] = b"\x02\x01\x10"  # area 1: armed away, sabotage
    data[6:9] = b"\x01\x01\x10"  # area 2: armed away, alarm (state 1)
    out = m.decode_partition_statuses(header + bytes(data))
    assert out[0] == m.NativeAreaStatus(
        mode=AreaMode.DISARMED, alarm=False, alarm_memory=False, sabotage=True
    )
    assert out[1] == m.NativeAreaStatus(
        mode=AreaMode.TOTAL, alarm=False, alarm_memory=False, sabotage=True
    )
    assert out[2] == m.NativeAreaStatus(
        mode=AreaMode.TOTAL, alarm=True, alarm_memory=True, sabotage=False
    )


async def test_zone_statuses_oserror_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_open(host: str, port: int):  # noqa: ANN202
        raise OSError("unreachable")

    monkeypatch.setattr(m.asyncio, "open_connection", fake_open)
    client = m.Local6004Client("host", "pass")
    with pytest.raises(m.Local6004Error, match="unreachable"):
        await client.async_get_zone_statuses({0})
    assert client._status_conn is None


# --------------------------------------------------------------- system status
def _system_data(**bits: tuple[int, int]) -> bytearray:
    data = bytearray(54)
    for byte, bit in bits.values():
        data[byte] |= 1 << bit
    return data


def _system_resp(data: bytes, supply: int = 243, battery: int = 134) -> bytes:
    data = bytearray(data)
    data[m._SYSTEM_SUPPLY_V : m._SYSTEM_SUPPLY_V + 2] = supply.to_bytes(2, "little")
    data[m._SYSTEM_BATTERY_V : m._SYSTEM_BATTERY_V + 2] = battery.to_bytes(2, "little")
    return (~0x0D & 0xFFFFFFFF).to_bytes(4, "little") + bytes(data)


def test_system_status_cmd_bytes() -> None:
    assert m._status_cmd(0x0D) == b"\x0d\x00\x00\x00\x00"
    assert m._status_cmd(0x31) == b"\x31\x00\x00\x00\x00"
    assert m._status_cmd(0x5E) == b"\x5e\x00\x00\x00" + bytes(32)
    assert m._STATUS_OPS == frozenset({6, 7, 0x0D, 0x31, 0x5E})


def test_decode_system_status_no_faults() -> None:
    status = m.decode_system_status(_system_resp(bytes(54)))
    assert status.faults == dict.fromkeys(m.FAULT_FLAG_KEYS, False)
    assert not status.has_faults
    assert status.supply_voltage == 24.3
    assert status.battery_voltage == 13.4
    assert not status.service
    assert not status.programming
    assert status.raw == bytes(12)


@pytest.mark.parametrize("key", list(m._SYSTEM_FAULT_BITS))
def test_decode_system_status_each_fault_bit(key: str) -> None:
    status = m.decode_system_status(_system_resp(_system_data(f=m._SYSTEM_FAULT_BITS[key])))
    assert {k for k, v in status.faults.items() if v} == {key}
    assert status.has_faults
    assert list(status.faults) == list(m.FAULT_FLAG_KEYS)


@pytest.mark.parametrize("source", list(m._SYSTEM_TAMPER_BITS))
def test_decode_system_status_device_tamper_raises_tamper(source: str) -> None:
    status = m.decode_system_status(_system_resp(_system_data(t=m._SYSTEM_TAMPER_BITS[source])))
    assert {k for k, v in status.faults.items() if v} == {"tamper"}
    assert status.has_faults
    assert {k for k, v in status.tamper_sources.items() if v} == {source}


def test_decode_system_status_volte() -> None:
    data = bytearray(54)
    assert not m.decode_system_status(_system_resp(bytes(data))).volte
    data[13] = 0x01
    assert m.decode_system_status(_system_resp(bytes(data))).volte


def test_decode_system_status_no_tamper_source() -> None:
    status = m.decode_system_status(_system_resp(bytes(54)))
    assert status.tamper_sources == dict.fromkeys(m._SYSTEM_TAMPER_BITS, False)


def _power_resp(temperature: int = 27, resistance: int = 115) -> bytes:
    data = bytearray(48)
    data[6:8] = temperature.to_bytes(2, "little")
    data[8:10] = resistance.to_bytes(2, "little")
    return (~0x31 & 0xFFFFFFFF).to_bytes(4, "little") + bytes(data)


def test_decode_power_values() -> None:
    values = m.decode_power_values(_power_resp())
    assert values == m.NativePowerValues(battery_temperature=27, battery_resistance=115)


def test_decode_power_values_not_reported() -> None:
    values = m.decode_power_values(_power_resp(0xFFFF, 0xFFFF))
    assert values == m.NativePowerValues(battery_temperature=None, battery_resistance=None)


def test_decode_power_values_short_raises() -> None:
    with pytest.raises(ValueError, match="short power-values"):
        m.decode_power_values(_power_resp()[:20])


def test_decode_system_status_unmapped_fault_bit_and_flags() -> None:
    # Fault word bit 0 (zone fuse) has no flag of its own but is still a fault.
    data = _system_data(fuse=(0, 0), serv=m._SYSTEM_SERVICE, prog=m._SYSTEM_PROGRAMMING)
    status = m.decode_system_status(_system_resp(data))
    assert not any(status.faults.values())
    assert status.has_faults
    assert status.service
    assert status.programming
    assert status.raw[0] == 0x01


def test_decode_system_status_rejects_short_response() -> None:
    with pytest.raises(ValueError, match="short"):
        m.decode_system_status(bytes(4 + 53))


async def test_system_status_over_the_persistent_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pairs = _patch_sessions(monkeypatch, [[_resp(_system_resp(bytes(54), supply=137))]])
    client = m.Local6004Client("host", "pass")
    status = await client.async_get_system_status()
    assert status.supply_voltage == 13.7
    assert _decrypt(pairs[0][1].sent[0]) == b"\x0d\x00\x00\x00\x00"


async def test_system_status_short_response_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sessions(monkeypatch, [[_resp(bytes(10))]])
    client = m.Local6004Client("host", "pass")
    with pytest.raises(m.Local6004Error, match="short"):
        await client.async_get_system_status()
    assert client._status_conn is None


async def test_power_values_over_the_persistent_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pairs = _patch_sessions(monkeypatch, [[_resp(_power_resp(26, 120))]])
    client = m.Local6004Client("host", "pass")
    values = await client.async_get_power_values()
    assert values == m.NativePowerValues(battery_temperature=26, battery_resistance=120)
    assert _decrypt(pairs[0][1].sent[0]) == b"\x31\x00\x00\x00\x00"


# A made-up identifier pattern: the decode must never carry any of it.
_FAKE_ID = b"9876543210123456789"


def _gsm_resp(
    result: int = 0, faults: int = 0, networks: int = 0x0E, signal: int = 67, operator: bytes = b"00101"
) -> bytes:
    data = bytearray(698)
    data[0:4] = bytes([result, faults, networks, signal])
    data[34 : 34 + 15] = _FAKE_ID[:15]  # IMSI
    data[54 : 54 + 19] = _FAKE_ID  # ICCID
    data[78 : 78 + len(operator)] = operator
    data[98:] = (b"Manufacturer: X\r\nIMEI: " + _FAKE_ID[:15]).ljust(600, b"\0")
    return (~0x5E & 0xFFFFFFFF).to_bytes(4, "little") + bytes(data)


def test_decode_gsm_status() -> None:
    status = m.decode_gsm_status(_gsm_resp())
    assert status == m.NativeGsmStatus(
        result=m.GSM_OK,
        faults=dict.fromkeys(m._GSM_FAULT_BITS, False),
        networks=("2G", "3G", "4G"),
        signal=67,
        operator_code="00101",
    )


def test_decode_gsm_status_never_carries_identifiers() -> None:
    status = m.decode_gsm_status(_gsm_resp())
    assert _FAKE_ID[:6].decode() not in repr(status)


@pytest.mark.parametrize(("bit", "name"), list(enumerate(m._GSM_FAULT_BITS)))
def test_decode_gsm_status_fault_bits(bit: int, name: str) -> None:
    status = m.decode_gsm_status(_gsm_resp(faults=1 << bit, networks=0x08))
    assert {k for k, v in status.faults.items() if v} == {name}
    assert status.networks == ("4G",)


@pytest.mark.parametrize("operator", [b"", b"--", b"1234", b"2220x"])
def test_decode_gsm_status_operator_code_only_when_valid(operator: bytes) -> None:
    assert m.decode_gsm_status(_gsm_resp(operator=operator)).operator_code == ""


def test_decode_gsm_status_short_raises() -> None:
    with pytest.raises(ValueError, match="short GSM-status"):
        m.decode_gsm_status(_gsm_resp()[:80])


async def test_gsm_status_over_the_persistent_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pairs = _patch_sessions(monkeypatch, [[_resp(_gsm_resp(result=m.GSM_NO_MODULE))]])
    client = m.Local6004Client("host", "pass")
    status = await client.async_get_gsm_status()
    assert status.result == m.GSM_NO_MODULE
    assert _decrypt(pairs[0][1].sent[0]) == b"\x5e\x00\x00\x00" + bytes(32)


# ----------------------------------------------------------------- code check
def _code_resp(code_type: int = 1) -> bytes:
    """A code-check (op 0x04) reply: 4-byte echo + 14 data bytes (type first)."""
    data = bytes([code_type]) + b"\x00\xff\xff\xff\x03\x00\x00\xff\xff\xff\xff\xff\xff"
    return (~m._OP_CHECK_CODE & 0xFFFFFFFF).to_bytes(4, "little") + data


def test_check_code_cmd_bytes() -> None:
    # [op:4 LE][no-PIN:6][code:6], the code one digit per byte padded 0xFF.
    assert m.check_code_cmd("123456") == (
        b"\x04\x00\x00\x00" + b"\x74\x00\x00\x00\x00\x00" + bytes([1, 2, 3, 4, 5, 6])
    )
    assert m.check_code_cmd("12") == (
        b"\x04\x00\x00\x00" + b"\x74\x00\x00\x00\x00\x00" + bytes([1, 2, 0xFF, 0xFF, 0xFF, 0xFF])
    )


def test_check_code_cmd_bad_code_raises() -> None:
    with pytest.raises(ValueError, match="PIN must be 1-6"):
        m.check_code_cmd("abc")
    with pytest.raises(ValueError, match="code is required"):
        m.check_code_cmd(None)  # type: ignore[arg-type]


def test_check_code_cmd_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    # op 0x04 must be in the query allow-list or the builder refuses it.
    monkeypatch.setattr(m, "_OP_CHECK_CODE", 3)
    with pytest.raises(m.ReadOnlyViolation):
        m.check_code_cmd("123456")


def test_decode_code_check_valid() -> None:
    result = m.decode_code_check(_code_resp(code_type=1))
    assert result == m.NativeCodeCheck(
        valid=True,
        code_type=1,
        raw=b"\x01\x00\xff\xff\xff\x03\x00\x00\xff\xff\xff\xff\xff\xff",
    )


@pytest.mark.parametrize("code_type", [0x00, 0xFF])
def test_decode_code_check_invalid(code_type: int) -> None:
    result = m.decode_code_check(_code_resp(code_type=code_type))
    assert result.valid is False
    assert result.code_type == code_type


def test_decode_code_check_short_raises() -> None:
    with pytest.raises(ValueError, match="short code-check"):
        m.decode_code_check(b"\xfb\xff\xff\xff")  # echo only, no type byte


async def test_check_code_over_the_persistent_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pairs = _patch_sessions(monkeypatch, [[_resp(_code_resp(code_type=1))]])
    client = m.Local6004Client("host", "pass")
    result = await client.async_check_code("123456")
    assert result.valid is True
    assert result.code_type == 1
    assert _decrypt(pairs[0][1].sent[0]) == (
        b"\x04\x00\x00\x00" + b"\x74\x00\x00\x00\x00\x00" + bytes([1, 2, 3, 4, 5, 6])
    )


async def test_check_code_rejects_a_bad_format_before_any_io() -> None:
    # No connection is patched: a malformed code must raise before opening one.
    client = m.Local6004Client("host", "pass")
    with pytest.raises(m.NativeCommandInvalid):
        await client.async_check_code("nope")


# --------------------------------------------------------------- output states
def test_decode_output_states() -> None:
    records = [
        _term(1),  # t1005: output off
        bytes([1, 3]) + bytes(8),  # t1006: output on (value 3)
        _term(0, b"\x08\x00\x01\x00"),  # t1007: a zone -> skipped
        _term(4),  # t1008: disabled -> skipped
    ]
    resp = bytes(m._STATUS_HEADER) + b"".join(records)
    assert m.decode_output_states(resp, 1005, 1009) == {1005: 0, 1006: 3}
    with pytest.raises(ValueError, match="short"):
        m.decode_output_states(resp, 1005, 1010)


async def test_output_states_chunks_and_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _terminal_resp([bytes([1, 1]) + bytes(8), _term(1), bytes([1, 2]) + bytes(8)])
    pairs = _patch_sessions(monkeypatch, [[resp]])
    client = m.Local6004Client("host", "pass")

    out = await client.async_get_output_states({1505, 1507})

    assert out == {1505: 1, 1507: 2}  # 1506 is read but not asked for
    sent = _decrypt(pairs[0][1].sent[0])
    assert sent[:4] == b"\x07\x00\x00\x00"
    assert sent[-4:] == (1505).to_bytes(2, "little") + (1508).to_bytes(2, "little")


async def test_output_states_error_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sessions(monkeypatch, [[_resp(bytes(m._STATUS_HEADER))]])
    client = m.Local6004Client("host", "pass")
    with pytest.raises(m.Local6004Error, match="short"):
        await client.async_get_output_states({1005})
    assert client._status_conn is None


# --------------------------------------------------------------- zone kinds
def test_zone_def_auto_bypass() -> None:
    zone = m.NativeZoneDef(1, "Zone", 1, (0,))
    assert not zone.auto_bypass  # options unknown
    assert replace(zone, options=m.ZONE_OPT_AUTO_BYPASS).auto_bypass
    assert not replace(zone, options=m.ZONE_OPT_INTERIOR).auto_bypass
