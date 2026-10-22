"""Forced arming for the INIM Prime integration.

Registers the ``inim_prime.arm_forced`` action: arm an area (or apply a
scenario) even when zones are open, by bypassing the open zones first. The
"force" is the identity of the action, never a flag smuggled into a standard
arm/select/button verb.

Safety model:
- **Fail closed by default**: if an open zone cannot be bypassed (programmed
  "Non escludibile", or the panel does not apply the bypass), nothing is armed
  and the call raises, naming the offending zones. ``allow_partial: true`` is
  an explicit opt-in to bypass what is possible and arm anyway, always
  reporting what stayed open.
- **Rollback**: a bypass that we applied is reverted if a later bypass or the
  arm fails before reaching the panel, so it is never left with zones excluded
  but disarmed. After an arm whose outcome is uncertain the bypasses stay: the
  panel may be armed, and re-including an open zone would trip it.
- **Verified bypass**: the zones are read back after the bypasses, so a
  silently ineffective one is classified as unbypassable rather than assumed.
- **Never silent**: every forced arm fires an ``inim_prime_forced_arm`` event.
- **The panel's own view**: the zones that matter are the ones the arming would
  check (see :func:`commands.zones_to_check`: auto-bypass zones and, for
  partial arming, interior zones are left out), read live natively.
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

from .client import (
    ArmMode,
    Local6004Error,
    NativeCommandError,
    NativeCommandNotSent,
    ZoneState,
)
from .commands import ARM_TO_AREA_MODE, scenario_modes, zones_to_check
from .const import DOMAIN, LOGGER
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


def _error(key: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


def _zone_list(coordinator: InimDataUpdateCoordinator, zone_ids: list[int]) -> list[dict[str, Any]]:
    """Render zones as response/event-friendly dicts."""
    labels = {zone.id: zone.label for zone in coordinator.data.zones}
    return [{"id": zone_id, "label": labels.get(zone_id, str(zone_id))} for zone_id in zone_ids]


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


async def _rollback(coordinator: InimDataUpdateCoordinator, zone_ids: list[int]) -> None:
    """Re-include zones we bypassed (best effort)."""
    for zone_id in zone_ids:
        try:
            await coordinator.client.async_set_zone_bypass(zone_id, False)
        except NativeCommandError as err:
            LOGGER.warning("Could not re-include zone %d after a forced arm: %s", zone_id, err)


async def async_force_arm(
    hass: HomeAssistant,
    entry: InimConfigEntry,
    *,
    kind: str,
    obj_id: int,
    mode: ArmMode | None = None,
    allow_partial: bool = False,
) -> dict[str, Any]:
    """Bypass the open zones, then arm the area / apply the scenario.

    Shared by the ``arm_forced`` action and the apply-scenario buttons (when the
    "force arm on open zones" switch is on). Fail-closed by default: if a zone
    cannot be bypassed and ``allow_partial`` is False, nothing is armed and it
    raises. Always fires ``inim_prime_forced_arm``.
    """
    coordinator = entry.runtime_data.coordinator
    client = coordinator.client
    if not client.supports_native_writes:
        raise _error("command_failed", error="native commands are not verified on this firmware")
    modes = (
        scenario_modes(coordinator, obj_id)
        if kind == "scenario"
        else {obj_id: ARM_TO_AREA_MODE[mode or ArmMode.TOTAL]}
    )
    zone_defs = {zone.id: zone for zone in coordinator.structure.zones or []}
    check = zones_to_check(zone_defs.values(), modes)

    try:
        statuses = await client.async_get_zone_statuses(check) if check else {}
        open_ids = sorted(
            zone_id
            for zone_id in check
            if (status := statuses.get(zone_id)) is None
            or (status.state is not ZoneState.READY and not status.excluded)
        )
        unbypassable = [zone_id for zone_id in open_ids if not zone_defs[zone_id].bypassable]
        tried: list[int] = []
        for zone_id in open_ids:
            if zone_id in unbypassable:
                continue
            try:
                await client.async_set_zone_bypass(zone_id, True)
            except NativeCommandNotSent:
                unbypassable.append(zone_id)
                continue
            except NativeCommandError:
                pass  # it may have been applied: the read-back below decides
            tried.append(zone_id)
        # Read back: a bypass the panel did not apply leaves the zone open.
        after = await client.async_get_zone_statuses(set(tried)) if tried else {}
    except Local6004Error as err:
        raise _error("command_failed", error=str(err)) from err
    bypassed = [zone_id for zone_id in tried if (s := after.get(zone_id)) is not None and s.excluded]
    unbypassable += [zone_id for zone_id in tried if zone_id not in bypassed]
    unbypassable.sort()

    if unbypassable and not allow_partial:
        await _rollback(coordinator, bypassed)
        labels = {zone.id: zone.label for zone in coordinator.data.zones}
        raise _error(
            "forced_arm_unbypassable_zones",
            zones=", ".join(labels.get(zone_id, str(zone_id)) for zone_id in unbypassable),
        )

    try:
        # The readiness was settled above: send without a second zone check.
        await client.async_set_area_modes(modes)
    except NativeCommandNotSent as err:
        await _rollback(coordinator, bypassed)
        raise _error("command_failed", error=str(err)) from err
    except NativeCommandError as err:
        # It may have armed: leave the bypasses (re-including an open zone of
        # an armed area would trip it) and let the user check the panel.
        await coordinator.async_refresh()
        raise _error("native_command_uncertain", error=str(err)) from err

    result: dict[str, Any] = {
        "armed": True,
        "kind": kind,
        "bypassed_zones": _zone_list(coordinator, bypassed),
        "unbypassable_zones": _zone_list(coordinator, unbypassable),
    }
    hass.bus.async_fire(EVENT_FORCED_ARM, {"entry_id": entry.entry_id, **result})
    await coordinator.async_refresh()
    return result


async def _handle_arm_forced(call: ServiceCall) -> ServiceResponse:
    """Handle the inim_prime.arm_forced action."""
    hass = call.hass
    entry, kind, obj_id, mode = _resolve_target(hass, call)
    return await async_force_arm(
        hass,
        entry,
        kind=kind,
        obj_id=obj_id,
        mode=mode,
        allow_partial=call.data[ATTR_ALLOW_PARTIAL],
    )


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
