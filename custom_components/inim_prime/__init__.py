"""The INIM Prime integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .areas import async_sync_area_registry, async_track_unselected_areas
from .client import InimPrimeClient, Local6004Client, Local6004Error
from .const import (
    CONF_APIKEY,
    CONF_LOCAL_PASSWORD,
    CONF_NATIVE_AREA_POLL,
    CONF_NATIVE_COMMANDS,
    CONF_USE_HTTPS,
    CONF_WEBHOOK_ENABLED,
    DEFAULT_NATIVE_AREA_POLL,
    DEFAULT_NATIVE_COMMANDS,
    DEFAULT_PORT,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_USE_HTTPS,
    DOMAIN,
    LOCAL_6004_PORT,
    LOGGER,
    NATIVE_AREA_POLL_INTERVAL,
    PLATFORMS,
)
from .coordinator import (
    InimConfigEntry,
    InimDataUpdateCoordinator,
    InimRuntimeData,
)
from .event_log import async_register_event_log_service
from .forced_arm import async_register_services
from .webhook import async_register_webhook, async_unregister_webhook

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up integration-wide services (registered once for the domain)."""
    async_register_services(hass)
    async_register_event_log_service(hass)
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

    # Read-only local (TCP 6004): read the static structure and scenario
    # definitions once (which objects exist and their labels, multi-active
    # scenes, zone->area, precise model). The channel must be reachable (a
    # failure aborts setup); a firmware whose layout is not mapped yet degrades
    # to cgi-only. It runs before the first cgi refresh so the first snapshot,
    # and so every entity, already uses the native structure when available.
    local_client = await _async_setup_local(hass, entry, coordinator)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = InimRuntimeData(
        client=client, coordinator=coordinator, local_client=local_client
    )

    if entry.options.get(CONF_WEBHOOK_ENABLED):
        async_register_webhook(hass, entry)

    native_poll = entry.options.get(CONF_NATIVE_AREA_POLL, DEFAULT_NATIVE_AREA_POLL)
    native_commands = entry.options.get(CONF_NATIVE_COMMANDS, DEFAULT_NATIVE_COMMANDS)
    if local_client is None:
        if native_poll or native_commands:
            LOGGER.warning(
                "Native area polling and native commands are off on this panel: its"
                " firmware is not supported over the native channel yet, so states and"
                " commands go over the cgi"
            )
    else:
        if native_commands and not local_client.supports_native_writes:
            # The panel firmware's native write responses are not verified yet
            # (e.g. 5.00): keep commands on the cgi rather than risk the panel.
            LOGGER.warning(
                "Native commands are on but not verified for this panel's firmware;"
                " arming, bypass, outputs and reset go over the cgi"
            )
        elif native_commands:
            # Opt-in native write commands (cgi fallback only when never sent).
            coordinator.command_client = local_client
            if not native_poll:
                LOGGER.info(
                    "Native commands are on without fast area and zone polling: after a"
                    " native command the state is only refreshed by the slower cgi poll"
                )
        if native_poll or native_commands:
            # Both share the persistent connection: close it on unload.
            entry.async_on_unload(local_client.async_close)
        if native_poll:
            # Fast area state over the native read-only status command.
            coordinator.async_attach_native(local_client)
            entry.async_on_unload(
                async_track_time_interval(
                    hass,
                    coordinator.async_native_poll,
                    timedelta(seconds=NATIVE_AREA_POLL_INTERVAL),
                    name="inim_prime native poll",
                    cancel_on_shutdown=True,
                )
            )

    # Apply the area selection before any platform adds entities: drop the
    # entities of areas that are not shown, and warn about panel areas added
    # after the selection was saved (they stay hidden until selected).
    async_sync_area_registry(hass, entry, coordinator.data.areas)
    entry.async_on_unload(async_track_unselected_areas(entry, coordinator))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def _async_setup_local(
    hass: HomeAssistant,
    entry: InimConfigEntry,
    coordinator: InimDataUpdateCoordinator,
) -> Local6004Client | None:
    """Read the static config over TCP 6004 once (read-only).

    The LAN password lives in entry.data (fallback: legacy entries that stored it
    in options). A connection/auth failure raises ConfigEntryNotReady (HA retries).
    A firmware whose native config layout is not mapped (anything but 4.x, e.g.
    the 5.00 released in 2026) is not an error: the integration then runs
    cgi-only (structure, states and commands from the cgi; no multi-active
    scenes, native outputs, fast polling or native commands) and returns None.
    """
    password = entry.data.get(CONF_LOCAL_PASSWORD) or entry.options.get(CONF_LOCAL_PASSWORD)
    if not password:
        raise ConfigEntryError(
            "The panel LAN password is required. Reconfigure the integration to add it."
        )
    local_client = Local6004Client(
        host=entry.data[CONF_HOST],
        password=password,
        port=LOCAL_6004_PORT,
    )
    try:
        config = await local_client.async_read_config()
    except Local6004Error as err:
        raise ConfigEntryNotReady(f"Local 6004 channel unreachable: {err}") from err
    if not config.layout_ok:
        LOGGER.warning(
            "Panel firmware '%s' is not supported over the native channel yet: running"
            " cgi-only (no multi-active scenes, native outputs, fast polling or native"
            " commands)",
            config.firmware,
        )
        coordinator.local_config = None
        return None
    coordinator.local_config = config
    structure = config.structure
    missing = [
        kind
        for kind in ("areas", "zones", "scenarios", "outputs")
        if structure is None or getattr(structure, kind) is None
    ]
    if missing:
        LOGGER.warning(
            "Could not read the panel %s over the native channel; using the cgi object"
            " list and names for them",
            ", ".join(missing),
        )
    LOGGER.debug(
        "Local 6004 active: %d scene definitions, native structure %s",
        len(config.scenes),
        structure,
    )
    return local_client


async def async_unload_entry(hass: HomeAssistant, entry: InimConfigEntry) -> bool:
    """Unload a config entry."""
    async_unregister_webhook(hass, entry)
    # Cancel any pending adaptive-poll decay timer so a webhook that fired just
    # before unload cannot re-arm polling on a torn-down coordinator.
    entry.runtime_data.coordinator.async_cancel_decay()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
