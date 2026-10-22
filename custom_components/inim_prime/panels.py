"""Multi-area alarm "panels": name-agnostic groupings of areas or scenarios.

A panel is one extra ``alarm_control_panel`` entity that arms or disarms several
partitions at once, so the dashboard can offer a single "arm the house" control
without inventing meaning from names. Panels are defined purely by partition ids
and scenario ids (never by labels like "Ins."/"Dis."): the user supplies the
display name.

Two kinds:

* ``AREAS`` — a set of partitions armed together. It offers all three arm modes
  (away/home/night = TOTAL/PARTIAL/SNAPSHOT) and disarm, each sent as one op 3
  setting every member partition to that mode.
* ``SCENARIOS`` — an arm scenario plus a disarm scenario (typically a detected
  structural inverse pair). It offers a single arm action (apply the arm
  scenario) and disarm (apply the disarm scenario), each sent as one op 3 with
  the scenario's own per-partition target modes.

The displayed state is always aggregated from the member partitions' live modes,
so it is honest whatever route set them (dashboard, keypad, scenario): disarmed
when all are disarmed, ``armed_<mode>`` when all share one armed mode, and
``armed_custom_bypass`` when they are mixed (some armed differently, or some
armed and some not), with the armed/disarmed members listed as attributes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from homeassistant.components.alarm_control_panel.const import AlarmControlPanelState

from .client import AreaMode, AreaState, SceneDef, scene_target_modes

# The arm services a panel can offer, and the partition mode each sets.
SERVICE_DISARM = "disarm"
SERVICE_ARM_AWAY = "arm_away"
SERVICE_ARM_HOME = "arm_home"
SERVICE_ARM_NIGHT = "arm_night"

_AREAS_SERVICE_MODE: dict[str, AreaMode] = {
    SERVICE_DISARM: AreaMode.DISARMED,
    SERVICE_ARM_AWAY: AreaMode.TOTAL,
    SERVICE_ARM_HOME: AreaMode.PARTIAL,
    SERVICE_ARM_NIGHT: AreaMode.SNAPSHOT,
}

_MODE_TO_STATE: dict[AreaMode, AlarmControlPanelState] = {
    AreaMode.DISARMED: AlarmControlPanelState.DISARMED,
    AreaMode.TOTAL: AlarmControlPanelState.ARMED_AWAY,
    AreaMode.PARTIAL: AlarmControlPanelState.ARMED_HOME,
    AreaMode.SNAPSHOT: AlarmControlPanelState.ARMED_NIGHT,
}


class PanelKind(StrEnum):
    """How a panel's membership and commands are defined."""

    AREAS = "areas"
    SCENARIOS = "scenarios"


@dataclass(frozen=True)
class PanelDef:
    """A panel definition, name-agnostic (ids only; the user names the panel)."""

    id: str
    kind: PanelKind
    name: str
    areas: tuple[int, ...] = ()
    arm_scenario: int | None = None
    disarm_scenario: int | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PanelDef:
        """Build a PanelDef from its stored options mapping."""
        return cls(
            id=str(data["id"]),
            kind=PanelKind(data["kind"]),
            name=str(data["name"]),
            areas=tuple(int(a) for a in data.get("areas", ())),
            arm_scenario=_opt_int(data.get("arm_scenario")),
            disarm_scenario=_opt_int(data.get("disarm_scenario")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain JSON-storable mapping for the entry options."""
        out: dict[str, Any] = {"id": self.id, "kind": self.kind.value, "name": self.name}
        if self.kind is PanelKind.AREAS:
            out["areas"] = list(self.areas)
        else:
            out["arm_scenario"] = self.arm_scenario
            out["disarm_scenario"] = self.disarm_scenario
        return out


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def load_panels(raw: Sequence[Mapping[str, Any]] | None) -> list[PanelDef]:
    """Parse the stored panel list, skipping any entry that cannot be read."""
    panels: list[PanelDef] = []
    for item in raw or ():
        try:
            panels.append(PanelDef.from_dict(item))
        except (KeyError, ValueError):
            continue
    return panels


def inverse_scenario_pairs(scenes: Sequence[SceneDef]) -> list[tuple[int, int]]:
    """Return ``(arm_id, disarm_id)`` scenario pairs that are structural inverses.

    A pair targets exactly the same partitions, one setting all of them to an
    armed mode and the other disarming all of them. Only cleanly-decoded scenes
    count (``scene_target_modes`` not None); labels are never consulted. At most
    one pair per partition set is returned (the first arm/disarm scene seen).
    """
    armed: dict[frozenset[int], int] = {}
    disarmed: dict[frozenset[int], int] = {}
    for scene in scenes:
        modes = scene_target_modes(scene.arms)
        if not modes:
            continue
        key = frozenset(modes)
        values = set(modes.values())
        if values == {AreaMode.DISARMED}:
            disarmed.setdefault(key, scene.id)
        elif AreaMode.DISARMED not in values:
            armed.setdefault(key, scene.id)
    return [(armed[key], disarmed[key]) for key in armed if key in disarmed]


def panel_members(panel: PanelDef, scenes_by_id: Mapping[int, SceneDef]) -> set[int]:
    """Return the partition ids a panel governs."""
    if panel.kind is PanelKind.AREAS:
        return set(panel.areas)
    members: set[int] = set()
    for scenario_id in (panel.arm_scenario, panel.disarm_scenario):
        if scenario_id is None:
            continue
        scene = scenes_by_id.get(scenario_id)
        if scene is not None:
            members |= set(scene_target_modes(scene.arms) or {})
    return members


def panel_services(panel: PanelDef) -> tuple[str, ...]:
    """Return the arm services (besides disarm) this panel offers.

    An areas panel offers all three modes; a scenarios panel offers a single
    arm action (apply the arm scenario), surfaced as arm-away.
    """
    if panel.kind is PanelKind.AREAS:
        return (SERVICE_ARM_AWAY, SERVICE_ARM_HOME, SERVICE_ARM_NIGHT)
    return (SERVICE_ARM_AWAY,)


def panel_command_modes(
    panel: PanelDef, service: str, scenes_by_id: Mapping[int, SceneDef]
) -> dict[int, AreaMode] | None:
    """Return the ``{partition: mode}`` to send for ``service``.

    None when the service does not apply to this panel or a referenced scenario
    is gone. ``service`` is one of the ``SERVICE_*`` constants.
    """
    if panel.kind is PanelKind.AREAS:
        mode = _AREAS_SERVICE_MODE.get(service)
        if mode is None or not panel.areas:
            return None
        return dict.fromkeys(panel.areas, mode)
    if service == SERVICE_DISARM:
        scenario_id = panel.disarm_scenario
    elif service == SERVICE_ARM_AWAY:
        scenario_id = panel.arm_scenario
    else:
        return None
    if scenario_id is None:
        return None
    scene = scenes_by_id.get(scenario_id)
    return scene_target_modes(scene.arms) if scene is not None else None


@dataclass(frozen=True)
class PanelState:
    """A panel's aggregated state and the members behind a mixed one."""

    state: AlarmControlPanelState | None
    armed_areas: tuple[int, ...] = ()
    disarmed_areas: tuple[int, ...] = ()


def aggregate_panel_state(
    members: set[int],
    modes_by_area: Mapping[int, AreaMode],
    states_by_area: Mapping[int, AreaState],
    memory_by_area: Mapping[int, bool],
) -> PanelState:
    """Aggregate the member partitions' live state into one panel state.

    * Unavailable (``state=None``) when the panel has no members, or any member
      is missing from the live data.
    * Triggered when any member is in alarm/sabotage, or armed with alarm memory.
    * Disarmed when all members are disarmed; ``armed_<mode>`` when all members
      share one armed mode; ``armed_custom_bypass`` when they are mixed, with the
      armed and disarmed members listed.
    """
    if not members or any(area_id not in modes_by_area for area_id in members):
        return PanelState(None)
    ids = sorted(members)
    triggered = any(
        states_by_area.get(area_id) in (AreaState.ALARM, AreaState.SABOTAGE)
        or (modes_by_area[area_id].is_armed and memory_by_area.get(area_id, False))
        for area_id in ids
    )
    if triggered:
        return PanelState(AlarmControlPanelState.TRIGGERED)
    modes = {modes_by_area[area_id] for area_id in ids}
    if modes == {AreaMode.DISARMED}:
        return PanelState(AlarmControlPanelState.DISARMED)
    if len(modes) == 1:
        return PanelState(_MODE_TO_STATE[next(iter(modes))])
    armed = tuple(a for a in ids if modes_by_area[a] is not AreaMode.DISARMED)
    disarmed = tuple(a for a in ids if modes_by_area[a] is AreaMode.DISARMED)
    return PanelState(
        AlarmControlPanelState.ARMED_CUSTOM_BYPASS,
        armed_areas=armed,
        disarmed_areas=disarmed,
    )
