"""Alarm control panel platform for the INIM Prime integration."""

from __future__ import annotations

from typing import Any

from .client import (
    ApiStatus,
    Area,
    AreaMode,
    AreaState,
    ArmMode,
    InimApiError,
    InimConnectionError,
)

from homeassistant.components.alarm_control_panel import AlarmControlPanelEntity
from homeassistant.components.alarm_control_panel.const import (
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, is_factory_default_area
from .coordinator import InimConfigEntry, InimDataUpdateCoordinator

# Arm/disarm issues panel writes; the cgi is single-threaded, so serialize
# commands to one in flight at a time.
PARALLEL_UPDATES = 1

_MODE_TO_STATE: dict[AreaMode, AlarmControlPanelState] = {
    AreaMode.DISARMED: AlarmControlPanelState.DISARMED,
    AreaMode.TOTAL: AlarmControlPanelState.ARMED_AWAY,
    AreaMode.PARTIAL: AlarmControlPanelState.ARMED_HOME,
    AreaMode.SNAPSHOT: AlarmControlPanelState.ARMED_NIGHT,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InimConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the INIM Prime alarm control panels from a config entry.

    Areas are added dynamically: a coordinator listener reconciles the set of
    entities against ``coordinator.data.areas`` on every update, so areas that
    appear after setup (e.g. a panel-side reconfiguration) show up without a
    reload. Removed areas are not deleted from the registry; their entity simply
    reports ``available = False`` (see :class:`InimAlarmControlPanel`).
    """
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def _sync() -> None:
        """Add an entity for every area id not yet known."""
        new_entities: list[InimAlarmControlPanel] = []
        for area in coordinator.data.areas:
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
        version = coordinator.data.version
        self._attr_unique_id = f"{entry.entry_id}_area_{area_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="INIM",
            model=version.primex,
            sw_version=version.version,
            name=entry.title,
        )
        label = self._area.label if self._area else None
        self._last_label = label
        # Hide unused, factory-default areas ("AREA 006"..) by default. The
        # gating is fixed at add time from the label the area first had.
        if label is not None and is_factory_default_area(label):
            self._attr_entity_registry_enabled_default = False

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

    def _raise_command_error(self, err: Exception) -> None:
        """Translate a panel write failure into a HomeAssistantError."""
        if isinstance(err, InimApiError) and err.status == ApiStatus.ZONES_NOT_READY:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="zones_not_ready",
            ) from err
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={"error": str(err)},
        ) from err

    async def _arm(self, mode: ArmMode, optimistic: AlarmControlPanelState) -> None:
        """Arm the area in the given mode, optimistically updating state."""
        try:
            await self.coordinator.client.arm_area(self._area_id, mode)
        except (InimApiError, InimConnectionError) as err:
            self._raise_command_error(err)
        self._optimistic(optimistic)
        await self.coordinator.async_request_refresh()

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Send disarm command."""
        try:
            await self.coordinator.client.disarm_area(self._area_id)
        except (InimApiError, InimConnectionError) as err:
            self._raise_command_error(err)
        self._optimistic(AlarmControlPanelState.DISARMED)
        await self.coordinator.async_request_refresh()

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Send arm away command."""
        await self._arm(ArmMode.TOTAL, AlarmControlPanelState.ARMED_AWAY)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Send arm home command."""
        await self._arm(ArmMode.PARTIAL, AlarmControlPanelState.ARMED_HOME)

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        """Send arm night command."""
        await self._arm(ArmMode.SNAPSHOT, AlarmControlPanelState.ARMED_NIGHT)
