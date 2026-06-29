from custom_components.inim_prime.client import (
    AreaMode,
    AreaState,
    ZoneState,
    ApiStats,
    Area,
    Fault,
    IpAcl,
    MacAcl,
    Output,
    Scenario,
    Timer,
    Version,
    Zone,
    parse_decimal,
)


def test_parse_decimal_handles_comma():
    assert parse_decimal("24,4") == 24.4


def test_version_from_raw(load_fixture):
    v = Version.from_raw(load_fixture("version")["data"])
    assert v.version == "1.0.1"
    assert v.primex == "4.07 PXxxx"
    assert v.servizio is False


def test_version_firmware_and_model_from_cgi(load_fixture):
    """Firmware comes from primex (4.07), NOT the API-version field (1.0.1);
    the generic 'PXxxx' template resolves to the friendly family name."""
    v = Version.from_raw(load_fixture("version")["data"])
    assert v.firmware == "4.07"
    assert v.model_name == "PrimeX"  # "PXxxx" has no real digits


def test_version_model_precise_variant():
    """A model string carrying digits (e.g. from the 6004 channel) is kept."""
    v = Version(version="1.0.1", verhttp="1.0.0", primex="4.07 PX020", servizio=False)
    assert v.firmware == "4.07"
    assert v.model_name == "PX020"


def test_version_firmware_falls_back_to_version_when_no_primex():
    """With an empty primex, firmware degrades to the version field."""
    v = Version(version="9.9", verhttp="1.0", primex="", servizio=False)
    assert v.firmware == "9.9"
    assert v.model_name == "PrimeX"


def test_zone_open_and_strip(load_fixture):
    zones = [Zone.from_raw(z) for z in load_fixture("zones")["data"]["zone"]]
    bagno = next(z for z in zones if z.id == 0)
    assert bagno.label == "Fin.Bagno PT"
    assert bagno.state is ZoneState.ALARM
    assert bagno.state.is_open is True
    assert bagno.excluded is False  # by == "1"


def test_area_disarmed(load_fixture):
    areas = [Area.from_raw(a) for a in load_fixture("partitions")["data"]["part"]]
    box = next(a for a in areas if a.id == 3)
    assert box.label == "Box"
    assert box.mode is AreaMode.DISARMED
    assert box.state is AreaState.READY


def test_scenario_active_flag(load_fixture):
    scenarios = [Scenario.from_raw(s) for s in load_fixture("scenarios")["data"]["sce"]]
    assert next(s for s in scenarios if s.label == "Dis.Totale").active is True
    assert next(s for s in scenarios if s.label == "Ins.Totale").active is False


def test_output_from_raw(load_fixture):
    outputs = [Output.from_raw(o) for o in load_fixture("outputs")["data"]["cmd"]]
    assert outputs[0].label == "Finestra Taverna"
    assert outputs[0].state == 0


def test_fault_no_faults(load_fixture):
    f = Fault.from_raw(load_fixture("faults")["data"])
    assert f.vcc == 24.4
    assert f.has_faults is False
    assert all(v is False for v in f.flags.values())


def test_fault_flags_int_bitmap():
    # low_battery is bit 2, fault_mains is bit 3 -> 0b1100 = 12.
    f = Fault.from_raw({"vcc": "13,7", "fau": 12})
    assert f.has_faults is True
    assert f.flags["low_battery"] is True
    assert f.flags["fault_mains"] is True
    assert f.flags["jam_radio"] is False
    # Exactly those two are set.
    assert [k for k, v in f.flags.items() if v] == ["low_battery", "fault_mains"]


def test_fault_flags_numeric_string_byte1():
    # tamper is bit 14 -> 1 << 14 = 16384, as a numeric string.
    f = Fault.from_raw({"vcc": "13,7", "fau": "16384"})
    assert f.has_faults is True
    assert f.flags["tamper"] is True
    assert [k for k, v in f.flags.items() if v] == ["tamper"]


def test_fault_flags_reserved_bits_ignored():
    # bits 0 and 1 are reserved available_* and must never surface as a flag.
    f = Fault.from_raw({"vcc": "13,7", "fau": 0b11})
    assert f.has_faults is True
    assert all(v is False for v in f.flags.values())


def test_fault_flags_flat_dict():
    f = Fault.from_raw({"vcc": "13,7", "fau": {"sirens": 1, "low_battery": 0}})
    assert f.flags["sirens"] is True
    assert f.flags["low_battery"] is False


def test_fault_flags_nested_dict():
    f = Fault.from_raw(
        {
            "vcc": "13,7",
            "fau": {
                "byte 0": {"low_battery": 1, "fault_mains": 0},
                "byte 1": {"tamper": 1},
            },
        }
    )
    assert f.flags["low_battery"] is True
    assert f.flags["fault_mains"] is False
    assert f.flags["tamper"] is True


def test_fault_flags_unknown_form_all_false():
    f = Fault.from_raw({"vcc": "13,7", "fau": "garbage"})
    # Unrecognized -> all flags False, but has_faults stays True (fau != "0").
    assert f.has_faults is True
    assert all(v is False for v in f.flags.values())


def test_timer_active_flag_and_strip(load_fixture):
    timers = [Timer.from_raw(t) for t in load_fixture("timers")["data"]["tmr"]]
    inactive = next(t for t in timers if t.id == 10)
    assert inactive.label == "TIMER      011"
    assert inactive.active is False
    active = next(t for t in timers if t.id == 0)
    assert active.label == "Fascia A"
    assert active.active is True


def test_api_stats_from_raw(load_fixture):
    stats = ApiStats.from_raw(load_fixture("status_api")["data"]["status"][0])
    assert stats.api == "unknown"
    assert stats.connections == 188
    assert isinstance(stats.connections, int)
    assert stats.last_connection == "18:20 24/06/2026"
    assert stats.last_ip == "192.168.85.25"


def test_ip_acl_drops_empty_slot(load_fixture):
    acl = IpAcl.from_raw(load_fixture("ip_auth")["data"])
    assert acl.only_enabled is False
    assert acl.ips == ["192.168.85.10"]


def test_mac_acl_drops_empty_slot(load_fixture):
    acl = MacAcl.from_raw(load_fixture("mac_auth")["data"])
    assert acl.only_enabled is True
    assert acl.macs == ["AA-BB-CC-DD-EE-FF"]


def test_open_zone_from_raw(load_fixture):
    from custom_components.inim_prime.client import OpenZone
    zones = [OpenZone.from_raw(z) for z in load_fixture("partitions_nrz")["data"]["nrz"]]
    assert zones[0].id == 0
    assert zones[0].label == "Fin.Bagno PT"
