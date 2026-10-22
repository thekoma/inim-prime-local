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
from .commands import async_arm_area
from .const import DOMAIN
from .coordinator import InimConfigEntry, InimDataUpdateCoordinator
from .device import panel_device_info

# Arm/disarm issues panel writes (see commands.py); serialize commands to one
# in flight at a time.
PARALLEL_UPDATES = 1

_MODE_TO_STATE: dict[AreaMode, AlarmControlPanelState] = {
    AreaMode.DISARMED: AlarmControlPanelState.DISARMED,
    AreaMode.TOTAL: AlarmControlPanelState.ARMED_AWAY,
    AreaMode.PARTIAL: AlarmControlPanelState.ARMED_HOME,
    AreaMode.SNAPSHOT: AlarmControlPanelState.ARMED_NIGHT,
}

# A user code is accepted as 1-6 digits (an INIM user PIN); the panel is the one
# that validates it (op 0x04). The frontend keypad uses this format.
_CODE_FORMAT = CodeFormat.NUMBER


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
        """Have the panel validate ``code`` when the require-code option is on.

        No-op when the option is off. Otherwise the code is checked against the
        panel (op 0x04, read-only) and never stored: a missing or malformed code
        raises :class:`ServiceValidationError`, one the panel rejects raises
        ``invalid_code``, and a panel that cannot be reached raises
        ``code_check_failed`` so the command is not sent on an unverified code.
        """
        if not self._require_code:
            return
        if not code:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="code_required")
        try:
            result = await self.coordinator.client.async_check_code(code)
        except Local6004Error as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="code_check_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if not result.valid:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_code")

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
