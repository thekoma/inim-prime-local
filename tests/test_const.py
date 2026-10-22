from custom_components.inim_prime.client import AreaMode, ZoneState


def test_zone_open_is_alarm_state():
    assert ZoneState.ALARM.is_open is True
    assert ZoneState.READY.is_open is False


def test_area_armed_flag():
    assert AreaMode.DISARMED.is_armed is False
    assert AreaMode.TOTAL.is_armed is True
