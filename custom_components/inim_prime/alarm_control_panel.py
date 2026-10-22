"""Alarm control panel platform for the INIM Prime integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.alarm_control_panel import AlarmControlPanelEntity
from homeassistant.components.alarm_control_panel.const import (
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .areas import area_shown
from .client import Area, AreaMode, AreaState, ArmMode, Local6004Error
from .commands import async_arm_area, async_set_areas
from .const import DOMAIN
from .coordinator import InimConfigEntry, InimDataUpdateCoordinator
from .device import panel_device_info
from .panels import (
    SERVICE_ARM_AWAY,
    SERVICE_ARM_HOME,
    SERVICE_ARM_NIGHT,
    SERVICE_DISARM,
    PanelDef,
    PanelState,
    aggregate_panel_state,
    panel_command_modes,
    panel_members,
    panel_services,
)

# Arm/disarm issues panel writes (see commands.py); serialize commands to one
# in flight at a time.
PARALLEL_UPDATES = 1

_MODE_TO_STATE: dict[AreaMode, AlarmControlPanelState] = {
    AreaMode.DISARMED: AlarmControlPanelState.DISARMED,
    AreaMode.TOTAL: AlarmControlPanelState.ARMED_AWAY,
    AreaMode.PARTIAL: AlarmControlPanelState.ARMED_HOME,
    AreaMode.SNAPSHOT: AlarmControlPanelState.ARMED_NIGHT,
}

# Panel arm services -> the HA feature flag each needs.
_SERVICE_FEATURE: dict[str, AlarmControlPanelEntityFeature] = {
    SERVICE_ARM_AWAY: AlarmControlPanelEntityFeature.ARM_AWAY,
    SERVICE_ARM_HOME: AlarmControlPanelEntityFeature.ARM_HOME,
    SERVICE_ARM_NIGHT: AlarmControlPanelEntityFeature.ARM_NIGHT,
}

# A user code is accepted as 1-6 digits (an INIM user PIN); the panel is the one
# that validates it (op 0x04). The frontend keypad uses this format.
_CODE_FORMAT = CodeFormat.NUMBER


async def _verify_code(
    coordinator: InimDataUpdateCoordinator, require_code: bool, code: str | None
) -> None:
    """Have the panel validate ``code`` when the require-code option is on.

    No-op when the option is off. Otherwise the code is checked against the panel
    (op 0x04, read-only) and never stored: a missing or malformed code raises
    :class:`ServiceValidationError`, one the panel rejects raises ``invalid_code``,
    and a panel that cannot be reached raises ``code_check_failed`` so no command
    is sent on an unverified code (fail-closed).
    """
    if not require_code:
        return
    if not code:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="code_required")
    try:
        result = await coordinator.client.async_check_code(code)
    except Local6004Error as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="code_check_failed",
            translation_placeholders={"error": str(err)},
        ) from err
    if not result.valid:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_code")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InimConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the INIM Prime alarm control panels from a config entry.

    Areas are added dynamically: a coordinator listener reconciles the set of
    entities against ``coordinator.data.areas`` on every update, so areas that
    appear after setup (e.g. a panel-side reconfiguration) show up without a
    reload. Only the areas the area selection shows get an entity (see areas.py).
    Removed areas are not deleted from the registry; their entity simply
    reports ``available = False`` (see :class:`InimAlarmControlPanel`).
    """
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def _sync() -> None:
        """Add an entity for every shown area id not yet known."""
        new_entities: list[InimAlarmControlPanel] = []
        for area in coordinator.data.areas:
            if not area_shown(hass, entry, area):
                continue
            entity = InimAlarmControlPanel(coordinator, area.id)
            uid = entity.unique_id
            if uid is None or uid in known:
                continue
            known.add(uid)
            new_entities.append(entity)
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_sync))
    _sync()

    # Multi-area panels are config-defined (not discovered from the panel), so the
    # set is fixed for this entry load; changing it reloads the entry.
    panels = [InimPanelAlarmControl(coordinator, panel) for panel in coordinator.panels]
    if panels:
        async_add_entities(panels)


class InimAlarmControlPanel(
    CoordinatorEntity[InimDataUpdateCoordinator], AlarmControlPanelEntity
):
    """Representation of a single INIM PrimeX area as an alarm panel."""

    _attr_has_entity_name = True
    _attr_code_arm_required = False
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )

    def __init__(
        self, coordinator: InimDataUpdateCoordinator, area_id: int
    ) -> None:
        """Initialize the alarm control panel."""
        super().__init__(coordinator)
        self._area_id = area_id
        self._optimistic_state: AlarmControlPanelState | None = None
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_area_{area_id}"
        self._attr_device_info = panel_device_info(coordinator)
        self._last_label = self._area.label if self._area else None
        # When the "require code" option is on, the panel validates an INIM user
        # code (op 0x04) before every arm and disarm; the frontend shows a keypad.
        self._require_code = coordinator.require_code
        if self._require_code:
            self._attr_code_arm_required = True
            self._attr_code_format = _CODE_FORMAT

    @property
    def _area(self) -> Area | None:
        """Return the current Area model from coordinator data."""
        return next(
            (a for a in self.coordinator.data.areas if a.id == self._area_id),
            None,
        )

    @property
    def name(self) -> str | None:
        """Return the live area label, falling back to the last-known one.

        Reading the current label from ``coordinator.data`` reflects panel-side
        renames without a reload. HA still lets the user override this with a
        custom name from the entity settings.
        """
        area = self._area
        if area is not None:
            self._last_label = area.label
        return self._last_label

    @property
    def available(self) -> bool:
        """Return True if the area is present in coordinator data."""
        return super().available and self._area is not None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Clear any optimistic state once fresh panel data arrives."""
        self._optimistic_state = None
        super()._handle_coordinator_update()

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        """Return the current alarm state for this area."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        area = self._area
        if area is None:
            return None
        if area.state in (AreaState.ALARM, AreaState.SABOTAGE):
            return AlarmControlPanelState.TRIGGERED
        if area.mode.is_armed and area.alarm_memory:
            return AlarmControlPanelState.TRIGGERED
        return _MODE_TO_STATE.get(area.mode)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes for this area."""
        area = self._area
        if area is None:
            return {"area_id": self._area_id}
        return {
            "area_id": area.id,
            "alarm_memory": area.alarm_memory,
            "mode": area.mode.name,
            "state": area.state.name,
        }

    @callback
    def _optimistic(self, state: AlarmControlPanelState) -> None:
        """Apply an optimistic state and notify HA immediately."""
        self._optimistic_state = state
        self.async_write_ha_state()

    async def _verify_code(self, code: str | None) -> None:
        """Have the panel validate ``code`` when the require-code option is on."""
        await _verify_code(self.coordinator, self._require_code, code)

    async def _arm(self, mode: ArmMode, optimistic: AlarmControlPanelState) -> None:
        """Arm (or disarm) the area, optimistically updating state.

        A failure raises a HomeAssistantError from commands.py; the state is
        re-read there right after the command.
        """
        await async_arm_area(self.coordinator, self._area_id, mode)
        self._optimistic(optimistic)

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Send disarm command."""
        await self._verify_code(code)
        await self._arm(ArmMode.DISARM, AlarmControlPanelState.DISARMED)

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Send arm away command."""
        await self._verify_code(code)
        await self._arm(ArmMode.TOTAL, AlarmControlPanelState.ARMED_AWAY)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Send arm home command."""
        await self._verify_code(code)
        await self._arm(ArmMode.PARTIAL, AlarmControlPanelState.ARMED_HOME)

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        """Send arm night command."""
        await self._verify_code(code)
        await self._arm(ArmMode.SNAPSHOT, AlarmControlPanelState.ARMED_NIGHT)


class InimPanelAlarmControl(
    CoordinatorEntity[InimDataUpdateCoordinator], AlarmControlPanelEntity
):
    """A multi-area "panel": one control that arms/disarms several partitions.

    Defined name-agnostically (areas or an arm/disarm scenario pair; see
    panels.py). Its state is aggregated from the member partitions' live modes,
    and each command is sent as one op 3 (zones checked, fail-closed), carrying a
    panel-verified code when the require-code option is on.
    """

    _attr_has_entity_name = True
    _attr_code_arm_required = False

    def __init__(self, coordinator: InimDataUpdateCoordinator, panel: PanelDef) -> None:
        """Initialize the panel entity from its definition."""
        super().__init__(coordinator)
        self._panel = panel
        self._optimistic_state: AlarmControlPanelState | None = None
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_panel_{panel.id}"
        self._attr_name = panel.name
        self._attr_device_info = panel_device_info(coordinator)
        features = AlarmControlPanelEntityFeature(0)
        for service in panel_services(panel):
            features |= _SERVICE_FEATURE[service]
        self._attr_supported_features = features
        self._require_code = coordinator.require_code
        if self._require_code:
            self._attr_code_arm_required = True
            self._attr_code_format = _CODE_FORMAT

    @property
    def _members(self) -> set[int]:
        return panel_members(self._panel, self.coordinator.scenes_by_id)

    @property
    def _panel_state(self) -> PanelState:
        areas = self.coordinator.data.areas
        return aggregate_panel_state(
            self._members,
            {area.id: area.mode for area in areas},
            {area.id: area.state for area in areas},
            {area.id: area.alarm_memory for area in areas},
        )

    @property
    def available(self) -> bool:
        """Available while every member partition is present in the live data."""
        return super().available and self._panel_state.state is not None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Clear any optimistic state once fresh panel data arrives."""
        self._optimistic_state = None
        super()._handle_coordinator_update()

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        """Return the aggregated alarm state of the member partitions."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        return self._panel_state.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the members and, for a mixed state, which are armed vs disarmed."""
        state = self._panel_state
        labels = {area.id: area.label for area in self.coordinator.data.areas}
        attrs: dict[str, Any] = {
            "panel_kind": self._panel.kind.value,
            "members": sorted(self._members),
        }
        if state.armed_areas or state.disarmed_areas:
            attrs["armed_areas"] = [labels.get(a, a) for a in state.armed_areas]
            attrs["disarmed_areas"] = [labels.get(a, a) for a in state.disarmed_areas]
        return attrs

    async def _apply(self, service: str) -> None:
        """Send the op 3 for ``service`` over all member partitions, then refresh.

        The optimistic state is the aggregate of the members' current modes with
        the command's targets applied, so a mixed scenario shows honestly at once.
        """
        modes = panel_command_modes(self._panel, service, self.coordinator.scenes_by_id)
        if not modes:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="panel_not_supported",
                translation_placeholders={"panel": self._panel.name},
            )
        await async_set_areas(self.coordinator, modes, f"panel {self._panel.id} {service}")
        live = {area.id: area.mode for area in self.coordinator.data.areas}
        self._optimistic_state = aggregate_panel_state(self._members, {**live, **modes}, {}, {}).state
        self.async_write_ha_state()

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Disarm every member partition (apply the disarm scenario, for a pair)."""
        await _verify_code(self.coordinator, self._require_code, code)
        await self._apply(SERVICE_DISARM)

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Arm away (areas), or apply the arm scenario (a scenario pair)."""
        await _verify_code(self.coordinator, self._require_code, code)
        await self._apply(SERVICE_ARM_AWAY)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Arm home (partial) every member partition (areas panels only)."""
        await _verify_code(self.coordinator, self._require_code, code)
        await self._apply(SERVICE_ARM_HOME)

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        """Arm night (snapshot) every member partition (areas panels only)."""
        await _verify_code(self.coordinator, self._require_code, code)
        await self._apply(SERVICE_ARM_NIGHT)
