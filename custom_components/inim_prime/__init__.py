"""The INIM Prime integration."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import InimPrimeClient
from .const import (
    CONF_APIKEY,
    CONF_USE_HTTPS,
    CONF_WEBHOOK_ENABLED,
    DEFAULT_PORT,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_USE_HTTPS,
    PLATFORMS,
)
from .coordinator import (
    InimConfigEntry,
    InimDataUpdateCoordinator,
    InimRuntimeData,
)
from .webhook import async_register_webhook, async_unregister_webhook


async def async_setup_entry(hass: HomeAssistant, entry: InimConfigEntry) -> bool:
    """Set up INIM Prime from a config entry."""
    session = async_get_clientsession(hass)
    client = InimPrimeClient(
        host=entry.data[CONF_HOST],
        port=entry.data.get(CONF_PORT, DEFAULT_PORT),
        apikey=entry.data[CONF_APIKEY],
        session=session,
        use_https=entry.data.get(CONF_USE_HTTPS, DEFAULT_USE_HTTPS),
        # Per-request ceiling for polling (and shared writes); a short
        # socket-connect timeout makes an unreachable panel fail fast.
        timeout=DEFAULT_REQUEST_TIMEOUT,
        connect_timeout=3,
    )

    coordinator = InimDataUpdateCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = InimRuntimeData(client=client, coordinator=coordinator)

    if entry.options.get(CONF_WEBHOOK_ENABLED):
        async_register_webhook(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: InimConfigEntry) -> bool:
    """Unload a config entry."""
    async_unregister_webhook(hass, entry)
    # Cancel any pending adaptive-poll decay timer so a webhook that fired just
    # before unload cannot re-arm polling on a torn-down coordinator.
    entry.runtime_data.coordinator.async_cancel_decay()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
