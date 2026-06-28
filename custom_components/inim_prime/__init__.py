"""The INIM Prime integration."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .client import InimPrimeClient, Local6004Client, Local6004Error
from .const import (
    CONF_APIKEY,
    CONF_LOCAL_ENABLED,
    CONF_LOCAL_PASSWORD,
    CONF_USE_HTTPS,
    CONF_WEBHOOK_ENABLED,
    DEFAULT_PORT,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_USE_HTTPS,
    DOMAIN,
    LOCAL_6004_PORT,
    LOGGER,
    PLATFORMS,
)
from .coordinator import (
    InimConfigEntry,
    InimDataUpdateCoordinator,
    InimRuntimeData,
)
from .forced_arm import async_register_services
from .webhook import async_register_webhook, async_unregister_webhook

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up integration-wide services (registered once for the domain)."""
    async_register_services(hass)
    return True


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

    # Optional read-only local (TCP 6004) enrichment: read the static scenario
    # definitions once so we can expose multi-active scene sensors. This must
    # never break setup — any failure degrades gracefully to cgi-only.
    local_client = await _async_setup_local(hass, entry, coordinator)

    entry.runtime_data = InimRuntimeData(
        client=client, coordinator=coordinator, local_client=local_client
    )

    if entry.options.get(CONF_WEBHOOK_ENABLED):
        async_register_webhook(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def _async_setup_local(
    hass: HomeAssistant,
    entry: InimConfigEntry,
    coordinator: InimDataUpdateCoordinator,
) -> Local6004Client | None:
    """Read the static config over TCP 6004 once (read-only), if enabled.

    Returns the client when enrichment is active, else None. Any failure
    (disabled, unreachable, wrong password, unsupported firmware) is logged and
    swallowed so the cgi-based integration keeps working unchanged.
    """
    if not (entry.options.get(CONF_LOCAL_ENABLED) and entry.options.get(CONF_LOCAL_PASSWORD)):
        return None
    local_client = Local6004Client(
        host=entry.data[CONF_HOST],
        password=entry.options[CONF_LOCAL_PASSWORD],
        port=LOCAL_6004_PORT,
    )
    try:
        config = await local_client.async_read_config()
    except Local6004Error as err:
        LOGGER.warning("Local 6004 read failed, continuing cgi-only: %s", err)
        return None
    if not config.layout_ok:
        LOGGER.warning(
            "Local 6004 firmware '%s' is not a supported 4.x layout; skipping scene enrichment",
            config.firmware,
        )
        return None
    coordinator.local_config = config
    LOGGER.debug(
        "Local 6004 enrichment active: %d scene definitions, %d zone->area maps",
        len(config.scenes),
        len(config.zone_areas),
    )
    return local_client


async def async_unload_entry(hass: HomeAssistant, entry: InimConfigEntry) -> bool:
    """Unload a config entry."""
    async_unregister_webhook(hass, entry)
    # Cancel any pending adaptive-poll decay timer so a webhook that fired just
    # before unload cannot re-arm polling on a torn-down coordinator.
    entry.runtime_data.coordinator.async_cancel_decay()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
