"""Which panel areas get entities (the "areas to show" option).

The selection is kept by area id, so a panel-side rename never changes it, and
the entity names keep following the live labels. Until the owner saves one,
every area with a real (non-factory) name is shown, plus any factory-named area
whose alarm panel the owner had enabled by hand under the old factory-name rule
(an upgrade must not drop an area someone uses).

An area that is not shown gets no entities at all: no alarm panel, no
alarm-memory sensor, no clear-memory button. It stays in the coordinator data,
though, so commands and scenarios still see every area (a scenario that also
sets a hidden area keeps its native path, see commands.py).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .client import Area
from .const import CONF_AREAS, CONF_AREAS_OFFERED, DOMAIN, LOGGER, is_factory_default_area
from .coordinator import InimDataUpdateCoordinator

# Per-area unique ids (after the "<entry_id>_" prefix), by entity domain.
_AREA_UNIQUE_IDS: dict[str, re.Pattern[str]] = {
    "alarm_control_panel": re.compile(r"area_(\d+)"),
    "binary_sensor": re.compile(r"area_memory_(\d+)"),
    "button": re.compile(r"button_(\d+)"),
}


def area_shown(hass: HomeAssistant, entry: ConfigEntry, area: Area) -> bool:
    """Return True if ``area`` gets entities under ``entry``'s area selection."""
    selected = entry.options.get(CONF_AREAS)
    if selected is not None:
        return str(area.id) in selected
    return not is_factory_default_area(area.label) or _enabled_by_hand(hass, entry, area.id)


def _enabled_by_hand(hass: HomeAssistant, entry: ConfigEntry, area_id: int) -> bool:
    """Return True if this area's alarm panel is registered and enabled.

    Under the old rule a factory-named area's entities were created disabled,
    so an enabled one was enabled by the owner on purpose.
    """
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "alarm_control_panel", DOMAIN, f"{entry.entry_id}_area_{area_id}"
    )
    return entity_id is not None and registry.entities[entity_id].disabled_by is None


def default_area_selection(
    hass: HomeAssistant, entry: ConfigEntry, areas: Iterable[Area]
) -> list[str]:
    """Return the ids shown while no selection is saved."""
    return [str(area.id) for area in areas if area_shown(hass, entry, area)]


def _area_id(entry: ConfigEntry, reg: er.RegistryEntry) -> int | None:
    """Return the area id of a per-area registry entry, or None for any other."""
    pattern = _AREA_UNIQUE_IDS.get(reg.domain)
    prefix = f"{entry.entry_id}_"
    if pattern is None or not reg.unique_id.startswith(prefix):
        return None
    match = pattern.fullmatch(reg.unique_id.removeprefix(prefix))
    return int(match.group(1)) if match else None


@callback
def async_sync_area_registry(hass: HomeAssistant, entry: ConfigEntry, areas: list[Area]) -> None:
    """Bring the registry in line with the area selection, before the platforms load.

    The entities of an area that is not shown are removed (not just disabled),
    so they never show up. An entity of a shown area that the old factory-name
    rule disabled is enabled again; one the owner disabled stays disabled.
    Without a saved selection, an area missing from ``areas`` is left alone: a
    short object list must not drop its entities.
    """
    registry = er.async_get(hass)
    by_id = {area.id: area for area in areas}
    selected = entry.options.get(CONF_AREAS)
    # Decide before changing anything: removing an area's alarm panel would
    # otherwise change the answer for its other two entities.
    entries: list[tuple[er.RegistryEntry, int, bool]] = []
    for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
        area_id = _area_id(entry, reg)
        if area_id is None:
            continue
        if selected is not None:
            entries.append((reg, area_id, str(area_id) in selected))
        elif (area := by_id.get(area_id)) is not None:
            entries.append((reg, area_id, area_shown(hass, entry, area)))
    for reg, area_id, shown in entries:
        if not shown:
            LOGGER.debug("Removing %s: area %d is not shown", reg.entity_id, area_id)
            registry.async_remove(reg.entity_id)
        elif reg.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
            registry.async_update_entity(reg.entity_id, disabled_by=None)


@callback
def async_track_unselected_areas(
    entry: ConfigEntry, coordinator: InimDataUpdateCoordinator
) -> Callable[[], None]:
    """Warn once per area added on the panel after the selection was saved.

    Such an area stays hidden until it is selected in the options; a factory
    named one is not worth a warning. Returns the listener's remove callback.
    """
    warned: set[int] = set()

    @callback
    def _check() -> None:
        selected = entry.options.get(CONF_AREAS)
        if selected is None:
            return
        offered = entry.options.get(CONF_AREAS_OFFERED, [])
        for area in coordinator.data.areas:
            key = str(area.id)
            if (
                key in selected
                or key in offered
                or area.id in warned
                or is_factory_default_area(area.label)
            ):
                continue
            warned.add(area.id)
            LOGGER.warning(
                "Panel area %s (number %d) is not shown in Home Assistant: it was added"
                " after the area selection was saved. Select it in the integration options"
                " to show it",
                area.label.strip(),
                area.id + 1,
            )

    _check()
    return coordinator.async_add_listener(_check)
