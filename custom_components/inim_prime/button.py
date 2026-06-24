"""Button platform for the INIM Prime integration."""

from __future__ import annotations

from .client import Area, InimApiError, InimConnectionError

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, is_factory_default_area
from .coordinator import InimConfigEntry, InimDataUpdateCoordinator

# Clearing alarm memory issues a panel write; the cgi is single-threaded, so
# serialize commands to one in flight at a time.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InimConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the INIM Prime button entities.

    Buttons are added dynamically via a coordinator listener so areas appearing
    after setup get a clear-alarm-memory button without a reload.
    """
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def _sync() -> None:
        """Add a button for every area id not yet known."""
        new_entities: list[InimClearAlarmMemoryButton] = []
        for area in coordinator.data.areas:
            entity = InimClearAlarmMemoryButton(coordinator, area)
            uid = entity.unique_id
            if uid is None or uid in known:
                continue
            known.add(uid)
            new_entities.append(entity)
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_sync))
    _sync()


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
        # Hide unused, factory-default areas ("AREA 006"..) by default. Gating
        # is fixed at add time from the label the area first had.
        if is_factory_default_area(area.label):
            self._attr_entity_registry_enabled_default = False
        entry = coordinator.config_entry
        version = coordinator.data.version
        self._attr_unique_id = f"{entry.entry_id}_button_{area.id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="INIM",
            model=version.primex,
            sw_version=version.version,
            name=entry.title,
        )

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
        """Clear the alarm memory for this area."""
        try:
            await self.coordinator.client.clear_alarm_memory(self._area_id)
        except (InimApiError, InimConnectionError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self.coordinator.async_request_refresh()
