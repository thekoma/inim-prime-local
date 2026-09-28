"""Panel write commands: native TCP 6004 when enabled, the cgi otherwise.

Entities and services call these instead of the cgi client so the channel
choice lives in one place. With the "native commands" option off (the
default) every call goes straight to the cgi, exactly as before.

With it on, the native command is tried first, and the fallback rules avoid
running a command twice:

* :class:`NativeCommandNotSent` (the frame never left, e.g. the connection
  could not be opened) falls back to the cgi, with a warning in the log;
* any other native failure (:class:`NativeCommandUncertain`, e.g. a timeout
  after sending, or an unexpected answer) may have been executed by the
  panel. It is **never** retried on the cgi: the state is re-read and a
  :class:`HomeAssistantError` tells the caller to check the panel.

Arming (any target mode other than disarmed) is sent natively only when the
fresh native zone state shows every non-bypassed zone of the target areas
ready. Otherwise the cgi is used, so the panel's own readiness check (and its
ZONES_NOT_READY answer) applies: how the native command treats open zones is
not known. Scenarios go native only when every target maps to one mode.

cgi failures propagate as :class:`InimApiError` / :class:`InimConnectionError`
so the callers keep translating them as before.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.exceptions import HomeAssistantError

from .client import (
    AreaMode,
    ArmMode,
    Local6004Client,
    NativeCommandError,
    NativeCommandNotSent,
    ZoneState,
    scene_target_modes,
)
from .const import DOMAIN, LOGGER
from .coordinator import InimDataUpdateCoordinator

_ARM_TO_AREA_MODE: dict[ArmMode, AreaMode] = {
    ArmMode.TOTAL: AreaMode.TOTAL,
    ArmMode.PARTIAL: AreaMode.PARTIAL,
    ArmMode.SNAPSHOT: AreaMode.SNAPSHOT,
    ArmMode.DISARM: AreaMode.DISARMED,
}

type _Native = Callable[[Local6004Client], Awaitable[None]]


async def _async_run(
    coordinator: InimDataUpdateCoordinator,
    what: str,
    native: _Native | None,
    cgi: Callable[[], Awaitable[object]],
) -> None:
    """Run ``native`` when enabled and possible, else ``cgi`` (see module doc)."""
    client = coordinator.command_client
    if client is None or native is None:
        await cgi()
        return
    try:
        await native(client)
    except (NativeCommandNotSent, ValueError) as err:
        # A ValueError is the client refusing the arguments before sending
        # (e.g. an id outside the native range): equally never sent.
        LOGGER.warning("Native %s was not sent (%s); sending it over the cgi", what, err)
        await cgi()
        return
    except NativeCommandError as err:
        LOGGER.warning("Native %s may have reached the panel: %s. Not retried", what, err)
        # Re-read both ways: the native poll may skip (a tick in flight, or
        # backing off), and the caller never reaches its own refresh.
        await coordinator.async_native_poll()
        await coordinator.async_request_refresh()
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="native_command_uncertain",
            translation_placeholders={"error": str(err)},
        ) from err
    LOGGER.debug("Native %s sent", what)
    # Show the result within ~10 ms instead of waiting for the next tick.
    await coordinator.async_native_poll()


def _ready_to_arm(coordinator: InimDataUpdateCoordinator, modes: dict[int, AreaMode]) -> bool:
    """Return True if the native state shows the armed targets ready.

    Needs a healthy native poll (fresh zone state) and the zone -> area map.
    A zone that is neither ready nor bypassed, in an area being armed, or
    with no known areas, makes this False: the cgi then decides.
    """
    armed = {area_id for area_id, mode in modes.items() if mode is not AreaMode.DISARMED}
    if not armed:
        return True
    local = coordinator.local_config
    if not coordinator.native_healthy or local is None or not local.zone_areas:
        return False
    for zone in coordinator.data.zones:
        if zone.excluded or zone.state is ZoneState.READY:
            continue
        areas = local.zone_areas.get(zone.id)
        if areas is None or armed.intersection(areas):
            return False
    return True


def _native_modes(
    coordinator: InimDataUpdateCoordinator, modes: dict[int, AreaMode]
) -> _Native | None:
    """Return the native op-3 call for ``modes``, or None to use the cgi."""
    if coordinator.command_client is None:
        return None
    known = {area.id for area in coordinator.data.areas}
    if not modes.keys() <= known or not _ready_to_arm(coordinator, modes):
        return None

    async def _send(client: Local6004Client) -> None:
        await client.async_set_area_modes(modes)

    return _send


async def async_arm_area(
    coordinator: InimDataUpdateCoordinator, area_id: int, mode: ArmMode
) -> None:
    """Arm (or, with ``ArmMode.DISARM``, disarm) one area."""
    native = _native_modes(coordinator, {area_id: _ARM_TO_AREA_MODE[mode]})

    async def _cgi() -> object:
        if mode is ArmMode.DISARM:
            return await coordinator.client.disarm_area(area_id)
        return await coordinator.client.arm_area(area_id, mode)

    await _async_run(coordinator, f"{mode.name.lower()} of area {area_id}", native, _cgi)


async def async_apply_scenario(coordinator: InimDataUpdateCoordinator, scenario_id: int) -> None:
    """Apply an arming scenario: natively as one op 3 when its targets map cleanly."""
    native: _Native | None = None
    local = coordinator.local_config
    scene = next(
        (s for s in (local.scenes if local is not None else []) if s.id == scenario_id), None
    )
    modes = scene_target_modes(scene.arms) if scene is not None else None
    if modes is not None:
        native = _native_modes(coordinator, modes)

    async def _cgi() -> object:
        return await coordinator.client.apply_scenario(scenario_id)

    await _async_run(coordinator, f"scenario {scenario_id}", native, _cgi)


async def async_clear_alarm_memory(coordinator: InimDataUpdateCoordinator, area_id: int) -> None:
    """Reset one area's alarm memory."""

    async def _native(client: Local6004Client) -> None:
        await client.async_reset_areas([area_id])

    async def _cgi() -> object:
        return await coordinator.client.clear_alarm_memory(area_id)

    await _async_run(coordinator, f"alarm memory reset of area {area_id}", _native, _cgi)


async def async_set_zone_excluded(
    coordinator: InimDataUpdateCoordinator, zone_id: int, excluded: bool
) -> None:
    """Bypass (exclude) or un-bypass one zone."""

    async def _native(client: Local6004Client) -> None:
        await client.async_set_zone_bypass(zone_id, excluded)

    async def _cgi() -> object:
        return await coordinator.client.set_zone_excluded(zone_id, excluded)

    action = "bypass" if excluded else "unbypass"
    await _async_run(coordinator, f"{action} of zone {zone_id}", _native, _cgi)


async def async_set_output(
    coordinator: InimDataUpdateCoordinator, output_id: int, value: int
) -> None:
    """Turn one panel output on (``value`` != 0) or off."""

    async def _native(client: Local6004Client) -> None:
        await client.async_set_output(output_id, value != 0)

    async def _cgi() -> object:
        return await coordinator.client.set_output(output_id, value)

    await _async_run(coordinator, f"output {output_id} -> {value}", _native, _cgi)
