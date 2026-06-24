"""Local webhook receiver for INIM Prime realtime panel events.

The panel's generic outbound HTTP client POSTs (or GETs) to a Home Assistant
local webhook on each configured event. The handler validates the request,
applies an idempotent optimistic patch to the coordinator's cached
``InimData`` snapshot, and notifies entities instantly via
``async_set_updated_data``. It then kicks a coordinator refresh as a
reconciliation backstop and never returns an error to the panel.

Security model (design doc §2.5): the cleartext-LAN URL embeds a per-entry
secret as the webhook id, which is the bearer token. Unknown ids are routed by
HA to a 200 with no handler, so an attacker cannot enumerate. We additionally
validate the event name and cap the body size, and always answer ``200`` even
on reject so we never leak which ids/params are valid.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from aiohttp import web

from homeassistant.components import webhook
from homeassistant.core import HomeAssistant

from .const import (
    KNOWN_EVENTS,
    LOGGER,
    MAX_WEBHOOK_BODY,
)

if TYPE_CHECKING:
    from .coordinator import InimConfigEntry


def async_register_webhook(hass: HomeAssistant, entry: InimConfigEntry) -> None:
    """Register the entry's webhook, if a webhook id is configured."""
    webhook_id = entry.data.get("webhook_id") or entry.options.get("webhook_id")
    if not webhook_id:
        return

    webhook.async_register(
        hass,
        "inim_prime",
        f"INIM Prime {entry.title}",
        webhook_id,
        _make_handler(entry),
        allowed_methods=["POST", "GET"],
        local_only=True,
    )


def async_unregister_webhook(hass: HomeAssistant, entry: InimConfigEntry) -> None:
    """Unregister the entry's webhook, if one was registered."""
    webhook_id = entry.data.get("webhook_id") or entry.options.get("webhook_id")
    if webhook_id:
        webhook.async_unregister(hass, webhook_id)


def _make_handler(
    entry: InimConfigEntry,
) -> Callable[[HomeAssistant, str, web.Request], Awaitable[web.Response]]:
    """Build a webhook handler bound to a config entry."""

    async def _handle(
        hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        """Handle one panel event. Always returns 200 (design doc §2.5)."""
        try:
            await _process(entry, request)
        except Exception:  # noqa: BLE001 — never error back to the panel.
            LOGGER.debug("INIM webhook: ignoring malformed request", exc_info=True)
        return web.Response(status=200)

    return _handle


async def _process(entry: InimConfigEntry, request: web.Request) -> None:
    """Parse, validate, and apply a single panel event."""
    params = await _read_params(request)
    ev = params.get("ev")

    if ev not in KNOWN_EVENTS:
        LOGGER.debug("INIM webhook: ignoring unknown event %r", ev)
        return

    runtime = entry.runtime_data
    coordinator = runtime.coordinator

    patched = coordinator.apply_event(ev, **_event_params(params))
    if patched is not None:
        coordinator.async_set_updated_data(patched)

    # Enter the fast poll tier so the panel is reconciled at the active cadence.
    # We deliberately do NOT request an immediate refresh: on a real panel the
    # poll lags the event (the whole reason for the optimistic push), so an
    # immediate re-read would re-fetch pre-event state and clobber the
    # optimistic patch within seconds, producing a visible flap. The scheduled
    # fast-tier poll reconciles once the panel has caught up.
    coordinator.activate_fast_poll()


async def _read_params(request: web.Request) -> dict[str, str]:
    """Merge query-string and (capped) form-body params into a flat dict.

    Query params take precedence; the body is only read when present and within
    the size cap.
    """
    merged: dict[str, str] = {}

    body_params: Mapping[str, object] = {}
    if request.method == "POST":
        raw = await request.read()
        if len(raw) > MAX_WEBHOOK_BODY:
            LOGGER.debug("INIM webhook: body too large (%d bytes), ignoring", len(raw))
        elif raw:
            try:
                body_params = await request.post()
            except Exception:  # noqa: BLE001
                body_params = {}

    for key, value in body_params.items():
        if isinstance(value, str):
            merged[key] = value
    # Query string wins over body on conflict.
    for key, value in request.query.items():
        merged[key] = value

    return merged


def _event_params(params: dict[str, str]) -> dict[str, str]:
    """Return only the patch-relevant params (drops ``ev``)."""
    return {k: v for k, v in params.items() if k != "ev"}
