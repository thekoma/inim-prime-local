"""Coverage-completing tests for the entity None-fallbacks.

Each entity must degrade cleanly when its backing panel object disappears
from the coordinator snapshot.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.inim_prime.alarm_control_panel import InimAlarmControlPanel
from custom_components.inim_prime.binary_sensor import (
    InimAreaAlarmMemoryBinarySensor,
    InimZoneBinarySensor,
)
from custom_components.inim_prime.client import (
    Area,
    AreaMode,
    AreaState,
    Fault,
    Output,
    Scenario,
    Version,
    Zone,
    ZoneState,
)
from custom_components.inim_prime.coordinator import (
    InimData,
)
from custom_components.inim_prime.switch import InimOutputSwitch, InimZoneBypassSwitch

VERSION = Version(raw="4.07 PX020", service=False)


def _data(**overrides) -> InimData:
    base = dict(
        version=VERSION,
        areas=[
            Area(
                id=1,
                label="Home",
                mode=AreaMode.DISARMED,
                state=AreaState.READY,
                alarm_memory=False,
            )
        ],
        zones=[
            Zone(
                id=1,
                label="Front Door",
                terminal=1,
                state=ZoneState.READY,
                alarm_memory=False,
                excluded=False,
            )
        ],
        scenarios=[Scenario(id=1, label="Away", active=False)],
        outputs=[Output(id=1, label="Siren", state=0)],
        fault=Fault(vcc=13.7, has_faults=False),
    )
    base.update(overrides)
    return InimData(**base)


def _fake_coordinator(data: InimData, client=None) -> SimpleNamespace:
    """A coordinator stand-in sufficient for CoordinatorEntity + our code."""
    return SimpleNamespace(
        data=data,
        client=client or AsyncMock(),
        last_update_success=True,
        local_config=None,
        async_request_refresh=AsyncMock(),
        async_add_listener=lambda *a, **k: lambda: None,
        config_entry=SimpleNamespace(entry_id="abc123", title="INIM Prime", options={}),
        hass=SimpleNamespace(config=SimpleNamespace(language="it")),
    )


# ---------------------------------------------------------------------------
# Entities — None-model fallbacks
# ---------------------------------------------------------------------------
def test_alarm_state_and_attrs_when_area_missing() -> None:
    """When the backing area disappears, alarm_state is None and attrs minimal."""
    coordinator = _fake_coordinator(_data(areas=[]))
    entity = InimAlarmControlPanel(coordinator, 99)
    assert entity.alarm_state is None
    assert entity.extra_state_attributes == {"area_id": 99}
    assert entity.available is False


def test_output_switch_none_when_output_missing() -> None:
    """A missing output reports is_on None, name None, unavailable."""
    coordinator = _fake_coordinator(_data(outputs=[]))
    entry = SimpleNamespace(entry_id="abc123", title="INIM Prime")
    entity = InimOutputSwitch(coordinator, entry, 99)
    assert entity.is_on is None
    assert entity.name is None
    assert entity.available is False


def test_zone_bypass_none_when_zone_missing() -> None:
    """A missing zone reports is_on None and name None."""
    coordinator = _fake_coordinator(_data(zones=[]))
    entry = SimpleNamespace(entry_id="abc123", title="INIM Prime")
    entity = InimZoneBypassSwitch(coordinator, entry, 99)
    assert entity.is_on is None
    assert entity.name is None


def test_binary_sensor_none_fallbacks() -> None:
    """Each binary sensor returns None when its backing model is absent."""
    coordinator = _fake_coordinator(_data(zones=[], areas=[], scenarios=[]))

    zone_bs = InimZoneBinarySensor(coordinator, 99)
    assert zone_bs.is_on is None
    assert zone_bs.extra_state_attributes is None

    area_bs = InimAreaAlarmMemoryBinarySensor(coordinator, 99)
    assert area_bs.is_on is None
