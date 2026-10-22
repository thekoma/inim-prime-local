"""Button platform for the INIM Prime integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .areas import area_shown
from .client import Area, Scenario
from .commands import async_apply_scenario, async_clear_alarm_memory, async_clear_calls
from .coordinator import InimConfigEntry, InimDataUpdateCoordinator
from .device import panel_device_info
from .forced_arm import async_force_arm

# Clearing alarm memory and applying a scenario are panel writes: serialize
# them to one in flight at a time.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InimConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the INIM Prime button entities.

    Buttons are added dynamically via a coordinator listener so areas appearing
    after setup get a clear-alarm-memory button without a reload. Only the areas
    the area selection shows get one (see areas.py).
    """
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def _sync() -> None:
        """Add buttons for area/scenario ids not yet known."""
        new_entities: list[ButtonEntity] = []

        def _add(entity: ButtonEntity) -> None:
            uid = entity.unique_id
            if uid is None or uid in known:
                return
            known.add(uid)
            new_entities.append(entity)

        _add(InimClearCallsButton(coordinator))
        for area in coordinator.data.areas:
            if area_shown(hass, entry, area):
                _add(InimClearAlarmMemoryButton(coordinator, area))
        # One "apply scenario" button per scenario, replacing the old single
        # active-scenario select (which couldn't express multi-active state).
        for scenario in coordinator.data.scenarios:
            _add(InimApplyScenarioButton(coordinator, scenario))

        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_sync))
    _sync()


class InimClearCallsButton(CoordinatorEntity[InimDataUpdateCoordinator], ButtonEntity):
    """Button that clears the panel's outgoing call queue (the keypad's "Canc. telefonate")."""

    _attr_has_entity_name = True
    _attr_translation_key = "clear_calls"

    def __init__(self, coordinator: InimDataUpdateCoordinator) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_clear_calls"
        self._attr_device_info = panel_device_info(coordinator)

    async def async_press(self) -> None:
        """Drop the pending calls and reports (a failure raises, see commands.py)."""
        await async_clear_calls(self.coordinator)


class InimClearAlarmMemoryButton(
    CoordinatorEntity[InimDataUpdateCoordinator], ButtonEntity
):
    """Button that clears the alarm memory of a single area."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: InimDataUpdateCoordinator, area: Area
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._area_id = area.id
        self._last_label = area.label
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_button_{area.id}"
        self._attr_device_info = panel_device_info(coordinator)

    @property
    def _area(self) -> Area | None:
        """Return the current area model from coordinator data."""
        return next(
            (a for a in self.coordinator.data.areas if a.id == self._area_id),
            None,
        )

    @property
    def available(self) -> bool:
        """Return True if the backing area is still present."""
        return super().available and self._area is not None

    @property
    def name(self) -> str | None:
        """Return the live "<area> clear alarm memory" name."""
        area = self._area
        if area is not None:
            self._last_label = area.label
        return f"{self._last_label} clear alarm memory"

    async def async_press(self) -> None:
        """Clear the alarm memory for this area (a failure raises, see commands.py)."""
        await async_clear_alarm_memory(self.coordinator, self._area_id)


class InimApplyScenarioButton(
    CoordinatorEntity[InimDataUpdateCoordinator], ButtonEntity
):
    """Button that applies (activates) a single arming scenario.

    Replaces the former single-value "active scenario" select: scenario state is
    now shown by the multi-active scene binary sensors, while activation is a
    stateless action — one button per scenario.
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: InimDataUpdateCoordinator, scenario: Scenario
    ) -> None:
        """Initialize the apply-scenario button."""
        super().__init__(coordinator)
        self._scenario_id = scenario.id
        self._last_label = scenario.label
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_scenario_apply_{scenario.id}"
        self._attr_device_info = panel_device_info(coordinator)

    @property
    def _scenario(self) -> Scenario | None:
        """Return the current scenario model from coordinator data."""
        return next(
            (s for s in self.coordinator.data.scenarios if s.id == self._scenario_id),
            None,
        )

    @property
    def available(self) -> bool:
        """Return True if the backing scenario is still present."""
        return super().available and self._scenario is not None

    @property
    def name(self) -> str | None:
        """Return the live scenario label, falling back to the last-known one."""
        scenario = self._scenario
        if scenario is not None:
            self._last_label = scenario.label
        return self._last_label

    async def async_press(self) -> None:
        """Activate this scenario on the panel.

        When the "force arm on open zones" switch is on, bypass the open zones
        first (fail-closed) instead of failing (see forced_arm.py). Otherwise
        apply it directly; a failure raises (see commands.py).
        """
        if self.coordinator.force_arm_on_open:
            # async_force_arm handles bypass/rollback/refresh and raises a
            # HomeAssistantError (e.g. unbypassable zones) on failure.
            await async_force_arm(
                self.hass,
                self.coordinator.config_entry,
                kind="scenario",
                obj_id=self._scenario_id,
            )
            return
        await async_apply_scenario(self.coordinator, self._scenario_id)
