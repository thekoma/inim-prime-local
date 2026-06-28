"""Forced arming for the INIM Prime integration.

Registers the ``inim_prime.arm_forced`` action: arm an area (or apply a
scenario) even when zones are open, by bypassing the open zones first. The
"force" is the identity of the action, never a flag smuggled into a standard
arm/select/button verb.

Safety model:
- **Fail closed by default**: if an open zone cannot be bypassed (panel rejects
  it, e.g. an unbypassable zone), nothing is armed and the call raises, naming
  the offending zones. ``allow_partial: true`` is an explicit opt-in to bypass
  what is possible and arm anyway, always reporting what stayed open.
- **Rollback**: a bypass that we applied is reverted if a later bypass, the
  arm/apply, or the readiness check fails, so the panel is never left with zones
  excluded but disarmed.
- **Verified bypass**: we re-read the zones after writing, so a silently
  ineffective bypass is classified as unbypassable rather than assumed applied.
- **Never silent**: every forced arm fires an ``inim_prime_forced_arm`` event.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .client import ApiStatus, ArmMode, InimApiError, InimConnectionError, OpenZone
from .const import DOMAIN
from .coordinator import InimConfigEntry, InimDataUpdateCoordinator

SERVICE_ARM_FORCED = "arm_forced"
EVENT_FORCED_ARM = "inim_prime_forced_arm"

ATTR_MODE = "mode"
ATTR_SCENARIO = "scenario"
ATTR_ALLOW_PARTIAL = "allow_partial"

_MODE_TO_ARM: dict[str, ArmMode] = {
    "away": ArmMode.TOTAL,
    "home": ArmMode.PARTIAL,
    "night": ArmMode.SNAPSHOT,
}

ARM_FORCED_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional(ATTR_MODE): vol.In(list(_MODE_TO_ARM)),
        vol.Optional(ATTR_SCENARIO): cv.string,
        vol.Optional(ATTR_ALLOW_PARTIAL, default=False): cv.boolean,
    }
)


def _zone_list(zones: list[OpenZone]) -> list[dict[str, Any]]:
    """Render open zones as response/event-friendly dicts."""
    return [{"id": z.id, "label": z.label} for z in zones]


def _resolve_entry(hass: HomeAssistant, entity_id: str) -> InimConfigEntry:
    """Find the INIM config entry that owns ``entity_id``."""
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None or entry.config_entry_id is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="forced_arm_bad_target",
            translation_placeholders={"entity_id": entity_id},
        )
    config_entry = hass.config_entries.async_get_entry(entry.config_entry_id)
    if config_entry is None or config_entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="forced_arm_bad_target",
            translation_placeholders={"entity_id": entity_id},
        )
    return config_entry


def _resolve_target(
    hass: HomeAssistant, call: ServiceCall
) -> tuple[InimConfigEntry, str, int, ArmMode | None]:
    """Resolve the service target to (entry, kind, object_id, mode).

    ``kind`` is "scenario" or "area". For a scenario the mode is None (the
    scenario carries its own modes); for an area it is the requested ArmMode.
    """
    entity_ids: list[str] = call.data[ATTR_ENTITY_ID]
    if len(entity_ids) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="forced_arm_single_target"
        )
    entity_id = entity_ids[0]
    entry = _resolve_entry(hass, entity_id)
    coordinator = entry.runtime_data.coordinator
    scenario_label = call.data.get(ATTR_SCENARIO)
    mode_key = call.data.get(ATTR_MODE)

    if scenario_label is not None:
        if mode_key is not None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="forced_arm_mode_with_scenario",
            )
        scenario = next(
            (s for s in coordinator.data.scenarios if s.label == scenario_label),
            None,
        )
        if scenario is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="forced_arm_unknown_scenario",
                translation_placeholders={"scenario": scenario_label},
            )
        return entry, "scenario", scenario.id, None

    # Area target: the entity must be one of this entry's alarm_control_panels.
    prefix = f"{entry.entry_id}_area_"
    registry = er.async_get(hass)
    reg_entry = registry.async_get(entity_id)
    if reg_entry is None or not (reg_entry.unique_id or "").startswith(prefix):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="forced_arm_area_or_scenario"
        )
    area_id = int(reg_entry.unique_id.removeprefix(prefix))
    mode = _MODE_TO_ARM[mode_key] if mode_key is not None else ArmMode.TOTAL
    return entry, "area", area_id, mode


async def _open_zones(
    coordinator: InimDataUpdateCoordinator, kind: str, obj_id: int, mode: ArmMode | None
) -> list[OpenZone]:
    """Return the zones that are not ready (open) for the target."""
    client = coordinator.client
    if kind == "scenario":
        return await client.get_scenario_open_zones(obj_id)
    return await client.get_area_open_zones(obj_id, mode or ArmMode.TOTAL)


async def _bypass_and_verify(
    coordinator: InimDataUpdateCoordinator, open_zones: list[OpenZone]
) -> tuple[list[OpenZone], list[OpenZone]]:
    """Try to bypass every open zone, then verify which actually took.

    Returns (bypassed, unbypassable). A zone is "unbypassable" if the write was
    rejected or the re-read shows it is still not excluded.
    """
    client = coordinator.client
    rejected: set[int] = set()
    for zone in open_zones:
        try:
            await client.set_zone_excluded(zone.id, True)
        except InimApiError:
            rejected.add(zone.id)
    # Re-read to confirm the bypass actually applied (a rejected/no-op write
    # leaves the zone not-excluded).
    current = {z.id: z.excluded for z in await client.get_zones()}
    bypassed = [z for z in open_zones if z.id not in rejected and current.get(z.id)]
    unbypassable = [z for z in open_zones if z not in bypassed]
    return bypassed, unbypassable


async def _rollback(
    coordinator: InimDataUpdateCoordinator, zones: list[OpenZone]
) -> None:
    """Re-include zones we previously bypassed (best effort)."""
    for zone in zones:
        try:
            await coordinator.client.set_zone_excluded(zone.id, False)
        except (InimApiError, InimConnectionError):
            pass


def _raise_command_error(err: Exception) -> None:
    """Translate a panel write failure into a HomeAssistantError."""
    if isinstance(err, InimApiError) and err.status == ApiStatus.ZONES_NOT_READY:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="zones_not_ready"
        ) from err
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="command_failed",
        translation_placeholders={"error": str(err)},
    ) from err


async def _handle_arm_forced(call: ServiceCall) -> ServiceResponse:
    """Handle the inim_prime.arm_forced action."""
    hass = call.hass
    entry, kind, obj_id, mode = _resolve_target(hass, call)
    coordinator = entry.runtime_data.coordinator
    allow_partial: bool = call.data[ATTR_ALLOW_PARTIAL]

    bypassed: list[OpenZone] = []
    unbypassable: list[OpenZone] = []
    open_zones = await _open_zones(coordinator, kind, obj_id, mode)
    if open_zones:
        bypassed, unbypassable = await _bypass_and_verify(coordinator, open_zones)
        if unbypassable and not allow_partial:
            await _rollback(coordinator, bypassed)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="forced_arm_unbypassable_zones",
                translation_placeholders={
                    "zones": ", ".join(z.label for z in unbypassable)
                },
            )

    try:
        if kind == "scenario":
            await coordinator.client.apply_scenario(obj_id)
        else:
            await coordinator.client.arm_area(obj_id, mode or ArmMode.TOTAL)
    except (InimApiError, InimConnectionError) as err:
        await _rollback(coordinator, bypassed)
        _raise_command_error(err)

    result: dict[str, Any] = {
        "armed": True,
        "kind": kind,
        "bypassed_zones": _zone_list(bypassed),
        "unbypassable_zones": _zone_list(unbypassable),
    }
    hass.bus.async_fire(EVENT_FORCED_ARM, {"entry_id": entry.entry_id, **result})
    await coordinator.async_request_refresh()
    return result


def async_register_services(hass: HomeAssistant) -> None:
    """Register the integration's domain-level actions (once)."""
    if hass.services.has_service(DOMAIN, SERVICE_ARM_FORCED):
        return
    hass.services.async_register(
        DOMAIN,
        SERVICE_ARM_FORCED,
        _handle_arm_forced,
        schema=ARM_FORCED_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
