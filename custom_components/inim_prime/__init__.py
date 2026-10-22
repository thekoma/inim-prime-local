"""The INIM Prime integration."""

from __future__ import annotations

from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .areas import async_sync_area_registry, async_track_unselected_areas
from .client import Local6004Client, Local6004Config, Local6004Error
from .const import (
    CONF_LOCAL_PASSWORD,
    CONF_WEBHOOK_ENABLED,
    DOMAIN,
    LEGACY_DATA_KEYS,
    LEGACY_OPTION_KEYS,
    LOCAL_6004_PORT,
    LOGGER,
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

# Sensors that only the panel's cgi could fill (its API version and connection
# statistics); removed from the registry of entries set up before 2026.10.7.
_LEGACY_SENSOR_KEYS = ("api_version", "api_connections", "api_last_ip")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up integration-wide services (registered once for the domain)."""
    async_register_services(hass)
    async_register_event_log_service(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: InimConfigEntry) -> bool:
    """Set up INIM Prime from a config entry (native TCP 6004 only)."""
    password = entry.data.get(CONF_LOCAL_PASSWORD) or entry.options.get(CONF_LOCAL_PASSWORD)
    if not password:
        raise ConfigEntryError(
            "The panel LAN password is required. Reconfigure the integration to add it."
        )
    client = Local6004Client(host=entry.data[CONF_HOST], password=password, port=LOCAL_6004_PORT)
    config = await _async_read_config(client)
    # The live reads and the commands share one persistent connection.
    entry.async_on_unload(client.async_close)

    coordinator = InimDataUpdateCoordinator(hass, entry, client, config)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = InimRuntimeData(client=client, coordinator=coordinator)

    if entry.options.get(CONF_WEBHOOK_ENABLED):
        async_register_webhook(hass, entry)

    # Apply the area selection before any platform adds entities: drop the
    # entities of areas that are not shown, and warn about panel areas added
    # after the selection was saved (they stay hidden until selected).
    async_sync_area_registry(hass, entry, coordinator.data.areas)
    entry.async_on_unload(async_track_unselected_areas(entry, coordinator))
    _async_remove_legacy_entities(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_read_config(client: Local6004Client) -> Local6004Config:
    """Read the panel's static config over TCP 6004 once (read-only).

    The firmware, the scenario definitions and the structure (which areas,
    zones, scenarios and outputs exist, and their names). An unreachable panel
    is retried by Home Assistant (ConfigEntryNotReady); a firmware whose memory
    map is not known is not supported (ConfigEntryError), and neither is a
    panel whose areas or zones could not be read.
    """
    try:
        config = await client.async_read_config()
    except Local6004Error as err:
        raise ConfigEntryNotReady(f"Panel unreachable over the native channel: {err}") from err
    if not config.layout_ok:
        raise ConfigEntryError(
            f"Panel firmware {config.firmware!r} is not supported: its memory map is not known"
        )
    structure = config.structure
    if structure is None or structure.areas is None or structure.zones is None:
        missing = [
            kind
            for kind in ("areas", "zones")
            if structure is None or getattr(structure, kind) is None
        ]
        raise ConfigEntryNotReady(f"Could not read the panel {' and '.join(missing)}")
    for kind in ("scenarios", "outputs"):
        if getattr(structure, kind) is None:
            LOGGER.warning("Could not read the panel %s; they are not available", kind)
    LOGGER.debug(
        "Native config: firmware %s, %d scene definitions, structure %s",
        config.firmware,
        len(config.scenes),
        structure,
    )
    return config


@callback
def _async_remove_legacy_entities(hass: HomeAssistant, entry: InimConfigEntry) -> None:
    """Remove entities only the cgi could fill, left by an earlier version."""
    registry = er.async_get(hass)
    for key in _LEGACY_SENSOR_KEYS:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_sensor_{key}")
        if entity_id is not None:
            registry.async_remove(entity_id)


async def async_migrate_entry(hass: HomeAssistant, entry: InimConfigEntry) -> bool:
    """Migrate a cgi-era entry (version 1) to the native-only configuration.

    The cgi connection settings and polling options are dropped, and the
    unique id becomes the host alone (it was ``host:cgi port``).
    """
    if entry.version == 1:
        data = {key: value for key, value in entry.data.items() if key not in LEGACY_DATA_KEYS}
        options = {
            key: value for key, value in entry.options.items() if key not in LEGACY_OPTION_KEYS
        }
        hass.config_entries.async_update_entry(
            entry, data=data, options=options, unique_id=data[CONF_HOST], version=2
        )
        LOGGER.info("Migrated the INIM Prime entry to the native-only configuration")
    return entry.version <= 2


async def async_unload_entry(hass: HomeAssistant, entry: InimConfigEntry) -> bool:
    """Unload a config entry."""
    async_unregister_webhook(hass, entry)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
