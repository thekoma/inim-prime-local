from custom_components.inim_prime.client import ApiStatus, AreaMode, Command, ZoneState


def test_status_codes_include_not_implemented():
    assert ApiStatus.SUCCESS == 0
    assert ApiStatus.NOT_IMPLEMENTED == 7


def test_zone_open_is_alarm_state():
    assert ZoneState.ALARM.is_open is True
    assert ZoneState.READY.is_open is False


def test_area_armed_flag():
    assert AreaMode.DISARMED.is_armed is False
    assert AreaMode.TOTAL.is_armed is True


def test_command_values_are_strings():
    assert Command.GET_ZONES == "get_zones_status"
    assert str(Command.VERSION) == "version"
