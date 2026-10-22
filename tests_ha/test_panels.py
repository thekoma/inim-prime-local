"""Tests for the name-agnostic multi-area panel logic (panels.py)."""

from __future__ import annotations

import pytest
from homeassistant.components.alarm_control_panel import AlarmControlPanelState

from custom_components.inim_prime.client import AreaMode, AreaState, SceneDef
from custom_components.inim_prime.panels import (
    SERVICE_ARM_AWAY,
    SERVICE_ARM_HOME,
    SERVICE_ARM_NIGHT,
    SERVICE_DISARM,
    PanelDef,
    PanelKind,
    aggregate_panel_state,
    inverse_scenario_pairs,
    load_panels,
    panel_command_modes,
    panel_members,
    panel_services,
)

_AREAS_PANEL = PanelDef(id="a1", kind=PanelKind.AREAS, name="House", areas=(0, 1, 2))
_SCEN_PANEL = PanelDef(
    id="s1", kind=PanelKind.SCENARIOS, name="All", arm_scenario=2, disarm_scenario=8
)
_SCENES = [
    SceneDef(id=2, arms={0: "away", 1: "away", 2: "away", 3: "away"}),
    SceneDef(id=8, arms={0: "disarm", 1: "disarm", 2: "disarm", 3: "disarm"}),
    SceneDef(id=5, arms={0: "away", 1: "away"}),  # armed set with no disarm inverse
    SceneDef(id=6, arms={0: "away+stay"}),  # un-decodable: ignored
    SceneDef(id=7, arms={}),  # empty: ignored
]
_SCENES_BY_ID = {s.id: s for s in _SCENES}


def test_inverse_scenario_pairs_finds_exact_inverses() -> None:
    assert inverse_scenario_pairs(_SCENES) == [(2, 8)]


def test_inverse_scenario_pairs_none_when_no_match() -> None:
    assert inverse_scenario_pairs([SceneDef(id=1, arms={0: "away"})]) == []


def test_panel_def_roundtrip() -> None:
    assert PanelDef.from_dict(_AREAS_PANEL.to_dict()) == _AREAS_PANEL
    assert PanelDef.from_dict(_SCEN_PANEL.to_dict()) == _SCEN_PANEL


def test_load_panels_skips_bad_entries() -> None:
    raw = [
        _AREAS_PANEL.to_dict(),
        {"id": "x", "kind": "nonsense", "name": "bad"},  # bad kind
        {"kind": "areas", "name": "no id"},  # missing id
        _SCEN_PANEL.to_dict(),
    ]
    assert load_panels(raw) == [_AREAS_PANEL, _SCEN_PANEL]
    assert load_panels(None) == []


def test_panel_members() -> None:
    assert panel_members(_AREAS_PANEL, _SCENES_BY_ID) == {0, 1, 2}
    assert panel_members(_SCEN_PANEL, _SCENES_BY_ID) == {0, 1, 2, 3}
    gone = PanelDef(id="g", kind=PanelKind.SCENARIOS, name="g", arm_scenario=99, disarm_scenario=8)
    assert panel_members(gone, _SCENES_BY_ID) == {0, 1, 2, 3}


def test_panel_members_skips_an_empty_scenario_slot() -> None:
    """A scenarios panel with only a disarm scenario still resolves its members."""
    panel = PanelDef(
        id="d", kind=PanelKind.SCENARIOS, name="d", arm_scenario=None, disarm_scenario=8
    )
    assert panel_members(panel, _SCENES_BY_ID) == {0, 1, 2, 3}


def test_panel_services() -> None:
    assert panel_services(_AREAS_PANEL) == (SERVICE_ARM_AWAY, SERVICE_ARM_HOME, SERVICE_ARM_NIGHT)
    assert panel_services(_SCEN_PANEL) == (SERVICE_ARM_AWAY,)


@pytest.mark.parametrize(
    ("service", "mode"),
    [
        (SERVICE_DISARM, AreaMode.DISARMED),
        (SERVICE_ARM_AWAY, AreaMode.TOTAL),
        (SERVICE_ARM_HOME, AreaMode.PARTIAL),
        (SERVICE_ARM_NIGHT, AreaMode.SNAPSHOT),
    ],
)
def test_panel_command_modes_areas(service: str, mode: AreaMode) -> None:
    assert panel_command_modes(_AREAS_PANEL, service, _SCENES_BY_ID) == dict.fromkeys(
        (0, 1, 2), mode
    )


def test_panel_command_modes_scenarios() -> None:
    away = panel_command_modes(_SCEN_PANEL, SERVICE_ARM_AWAY, _SCENES_BY_ID)
    assert away == dict.fromkeys((0, 1, 2, 3), AreaMode.TOTAL)
    disarm = panel_command_modes(_SCEN_PANEL, SERVICE_DISARM, _SCENES_BY_ID)
    assert disarm == dict.fromkeys((0, 1, 2, 3), AreaMode.DISARMED)
    # A scenarios panel offers no home/night.
    assert panel_command_modes(_SCEN_PANEL, SERVICE_ARM_HOME, _SCENES_BY_ID) is None


def test_panel_command_modes_none_cases() -> None:
    empty = PanelDef(id="e", kind=PanelKind.AREAS, name="e", areas=())
    assert panel_command_modes(empty, SERVICE_ARM_AWAY, _SCENES_BY_ID) is None
    gone = PanelDef(id="g", kind=PanelKind.SCENARIOS, name="g", arm_scenario=99, disarm_scenario=8)
    assert panel_command_modes(gone, SERVICE_ARM_AWAY, _SCENES_BY_ID) is None
    no_arm = PanelDef(id="n", kind=PanelKind.SCENARIOS, name="n", disarm_scenario=8)
    assert panel_command_modes(no_arm, SERVICE_ARM_AWAY, _SCENES_BY_ID) is None


def _ready(ids):  # noqa: ANN001, ANN202
    return dict.fromkeys(ids, AreaState.READY)


def test_aggregate_unavailable_when_empty_or_member_missing() -> None:
    assert aggregate_panel_state(set(), {}, {}, {}).state is None
    modes = {0: AreaMode.TOTAL, 1: AreaMode.TOTAL}
    assert aggregate_panel_state({0, 1, 9}, modes, _ready((0, 1)), {}).state is None


def test_aggregate_all_disarmed() -> None:
    modes = dict.fromkeys((0, 1, 2), AreaMode.DISARMED)
    assert aggregate_panel_state({0, 1, 2}, modes, _ready((0, 1, 2)), {}).state == (
        AlarmControlPanelState.DISARMED
    )


@pytest.mark.parametrize(
    ("mode", "state"),
    [
        (AreaMode.TOTAL, AlarmControlPanelState.ARMED_AWAY),
        (AreaMode.PARTIAL, AlarmControlPanelState.ARMED_HOME),
        (AreaMode.SNAPSHOT, AlarmControlPanelState.ARMED_NIGHT),
    ],
)
def test_aggregate_all_one_mode(mode: AreaMode, state: AlarmControlPanelState) -> None:
    modes = dict.fromkeys((0, 1, 2), mode)
    assert aggregate_panel_state({0, 1, 2}, modes, _ready((0, 1, 2)), {}).state == state


def test_aggregate_mixed_is_custom_bypass_with_members() -> None:
    modes = {0: AreaMode.TOTAL, 1: AreaMode.DISARMED, 2: AreaMode.PARTIAL}
    result = aggregate_panel_state({0, 1, 2}, modes, _ready((0, 1, 2)), {})
    assert result.state == AlarmControlPanelState.ARMED_CUSTOM_BYPASS
    assert result.armed_areas == (0, 2)
    assert result.disarmed_areas == (1,)


def test_aggregate_triggered_by_alarm_state() -> None:
    modes = dict.fromkeys((0, 1), AreaMode.TOTAL)
    states = {0: AreaState.ALARM, 1: AreaState.READY}
    assert aggregate_panel_state({0, 1}, modes, states, {}).state == (
        AlarmControlPanelState.TRIGGERED
    )


def test_aggregate_triggered_by_armed_memory() -> None:
    modes = dict.fromkeys((0, 1), AreaMode.TOTAL)
    assert aggregate_panel_state({0, 1}, modes, _ready((0, 1)), {0: True}).state == (
        AlarmControlPanelState.TRIGGERED
    )
