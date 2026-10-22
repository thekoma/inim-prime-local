"""Panel write commands, over the panel's native protocol (TCP 6004).

Entities and services call these so the command logic lives in one place.
Every command is sent natively, once:

* :class:`NativeCommandNotSent` (the frame never left, e.g. the connection
  could not be opened) means nothing reached the panel: the caller gets a
  ``command_failed`` error;
* any other native failure (:class:`NativeCommandUncertain`, e.g. a timeout
  after sending, or an unexpected answer) may have been executed by the
  panel. It is **never** retried: the state is re-read and a
  :class:`HomeAssistantError` tells the caller to check the panel.

Arming (any target mode other than disarmed) reads the zones that matter for
it natively, under the same lock and right before sending, and sends only if
every one is ready or bypassed; otherwise nothing is sent and the caller gets
a zones-not-ready error. The zones that matter are those of the areas being
armed, less the ones the panel ignores for that arming (see
:func:`zones_to_check`). Tamper, faults and alarm memory are not checked.
Scenarios are sent as one op 3 when every target maps to one mode and the
panel has no partition beyond the decoded scene range (0-11).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable

from homeassistant.exceptions import HomeAssistantError

from .client import (
    SCENE_DECODED_PARTITIONS,
    AreaMode,
    ArmMode,
    Local6004Client,
    Local6004Error,
    NativeCommandError,
    NativeCommandNotSent,
    NativeZoneDef,
    NativeZonesNotReady,
    scene_target_modes,
)
from .const import DOMAIN, LOGGER
from .coordinator import InimDataUpdateCoordinator

ARM_TO_AREA_MODE: dict[ArmMode, AreaMode] = {
    ArmMode.TOTAL: AreaMode.TOTAL,
    ArmMode.PARTIAL: AreaMode.PARTIAL,
    ArmMode.SNAPSHOT: AreaMode.SNAPSHOT,
    ArmMode.DISARM: AreaMode.DISARMED,
}
# Arming modes that leave the interior ("Interna") zones out.
_INTERIOR_FREE = frozenset({AreaMode.PARTIAL, AreaMode.SNAPSHOT})

type _Native = Callable[[Local6004Client], Awaitable[None]]

# A native bypass is read back this many times, this far apart (seconds).
_BYPASS_CHECKS = 4
_BYPASS_CHECK_DELAY = 0.5


def _error(key: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


async def _async_run(coordinator: InimDataUpdateCoordinator, what: str, native: _Native) -> None:
    """Send ``native`` once and show its result (see the module docstring)."""
    client = coordinator.client
    if not client.supports_native_writes:
        raise _error("command_failed", error="native commands are not verified on this firmware")
    try:
        await native(client)
    except NativeZonesNotReady as err:
        # The pre-send check read the zones that matter live and found one open.
        LOGGER.debug("Native %s not sent (%s); zones not ready", what, err)
        raise _error("zones_not_ready") from err
    except NativeCommandNotSent as err:
        LOGGER.warning("Native %s was not sent: %s", what, err)
        raise _error("command_failed", error=str(err)) from err
    except NativeCommandError as err:
        LOGGER.warning("Native %s may have reached the panel: %s. Not retried", what, err)
        await coordinator.async_refresh()
        raise _error("native_command_uncertain", error=str(err)) from err
    LOGGER.debug("Native %s sent", what)
    # Show the result right away instead of waiting for the next cycle.
    await coordinator.async_refresh()


def zones_to_check(zones: Iterable[NativeZoneDef], modes: dict[int, AreaMode]) -> set[int]:
    """Return the zones that must be ready (or bypassed) before arming ``modes``.

    They are the zones of every area being armed (not disarmed), except those
    the panel itself leaves out of that arming: an auto-bypass zone
    ("Autoescludibile") is bypassed by the panel when open, and an interior
    zone ("Interna") is not armed when every area it is armed with goes to
    partial or snapshot.
    """
    armed = {area_id: mode for area_id, mode in modes.items() if mode is not AreaMode.DISARMED}
    check: set[int] = set()
    for zone in zones:
        targets = [armed[area_id] for area_id in zone.areas if area_id in armed]
        if not targets or zone.auto_bypass:
            continue
        if zone.is_interior and all(mode in _INTERIOR_FREE for mode in targets):
            continue
        check.add(zone.id)
    return check


def _area_modes(coordinator: InimDataUpdateCoordinator, modes: dict[int, AreaMode]) -> _Native:
    """Return the native op-3 call setting ``modes`` (zones checked when arming)."""
    known = {area.id for area in coordinator.data.areas}
    if not modes.keys() <= known:
        raise _error("command_failed", error=f"unknown area {sorted(modes.keys() - known)}")
    zones = zones_to_check(coordinator.structure.zones or [], modes)

    async def _send(client: Local6004Client) -> None:
        await client.async_set_area_modes(modes, require_ready=zones)

    return _send


async def async_arm_area(
    coordinator: InimDataUpdateCoordinator, area_id: int, mode: ArmMode
) -> None:
    """Arm (or, with ``ArmMode.DISARM``, disarm) one area."""
    native = _area_modes(coordinator, {area_id: ARM_TO_AREA_MODE[mode]})
    await _async_run(coordinator, f"{mode.name.lower()} of area {area_id}", native)


async def async_set_areas(
    coordinator: InimDataUpdateCoordinator, modes: dict[int, AreaMode], what: str
) -> None:
    """Set several areas' modes in one op 3 (zones checked, fail-closed).

    Used by the multi-area panels to arm or disarm their member partitions
    together; ``what`` is a short label for the logs.
    """
    native = _area_modes(coordinator, modes)
    await _async_run(coordinator, what, native)


def scenario_modes(
    coordinator: InimDataUpdateCoordinator, scenario_id: int
) -> dict[int, AreaMode]:
    """Return what a scenario sets, as ``{area: mode}``; raise if it cannot go natively.

    Scene definitions only cover partitions 0-11, so on a panel with more a
    scenario might also set partitions the decode cannot see; a scenario with a
    combined or unknown target mode has no single mode per area.
    """
    scene = next((s for s in coordinator.local_config.scenes if s.id == scenario_id), None)
    modes = scene_target_modes(scene.arms) if scene is not None else None
    decoded = all(area.id < SCENE_DECODED_PARTITIONS for area in coordinator.data.areas)
    if modes is None or not decoded:
        raise _error("scenario_not_supported")
    return modes


async def async_apply_scenario(coordinator: InimDataUpdateCoordinator, scenario_id: int) -> None:
    """Apply an arming scenario as one op 3 setting all its areas."""
    native = _area_modes(coordinator, scenario_modes(coordinator, scenario_id))
    await _async_run(coordinator, f"scenario {scenario_id}", native)


async def async_clear_alarm_memory(coordinator: InimDataUpdateCoordinator, area_id: int) -> None:
    """Reset one area's alarm memory."""

    async def _native(client: Local6004Client) -> None:
        await client.async_reset_areas([area_id])

    await _async_run(coordinator, f"alarm memory reset of area {area_id}", _native)


async def async_clear_calls(coordinator: InimDataUpdateCoordinator) -> None:
    """Clear the panel's outgoing call queue (pending calls and reports)."""
    area_ids = [area.id for area in coordinator.data.areas]

    async def _native(client: Local6004Client) -> None:
        await client.async_clear_calls(area_ids)

    await _async_run(coordinator, "call queue clear", _native)


async def async_set_zone_excluded(
    coordinator: InimDataUpdateCoordinator, zone_id: int, excluded: bool
) -> None:
    """Bypass (exclude) or un-bypass one zone.

    The zone is then read back live: the panel acknowledges a bypass it does
    not apply, so a change that never shows up is reported instead of the
    switch silently flipping back.
    """

    async def _native(client: Local6004Client) -> None:
        await client.async_set_zone_bypass(zone_id, excluded)
        if not await _bypass_applied(client, zone_id, excluded):
            await coordinator.async_refresh()
            label = next((z.label for z in coordinator.data.zones if z.id == zone_id), zone_id)
            raise _error("bypass_not_applied", zone=str(label))

    action = "bypass" if excluded else "unbypass"
    await _async_run(coordinator, f"{action} of zone {zone_id}", _native)


async def async_set_output(
    coordinator: InimDataUpdateCoordinator, output_id: int, value: int
) -> None:
    """Turn one panel output on (``value`` != 0) or off."""

    async def _native(client: Local6004Client) -> None:
        await client.async_set_output(output_id, value != 0)

    await _async_run(coordinator, f"output {output_id} -> {value}", _native)


async def _bypass_applied(client: Local6004Client, zone_id: int, excluded: bool) -> bool:
    """Return False only if the zone's live status never shows ``excluded``.

    A status that cannot be read says nothing either way: the regular poll then
    shows the truth.
    """
    for attempt in range(_BYPASS_CHECKS):
        if attempt:
            await asyncio.sleep(_BYPASS_CHECK_DELAY)
        try:
            status = (await client.async_get_zone_statuses({zone_id})).get(zone_id)
        except Local6004Error:
            return True
        if status is None or status.excluded is excluded:
            return True
    return False
