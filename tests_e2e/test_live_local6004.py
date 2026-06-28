"""End-to-end for the read-only local protocol (TCP 6004) against the REAL panel.

Read-only: only reads the static scenario definitions + zone->area + firmware, and
verifies the multi-active scene sensors appear when the integration is set up with
the local option enabled. Skipped unless INIM_LOCAL_PASSWORD is set in the env.
"""

from __future__ import annotations

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import Local6004Client
from custom_components.inim_prime.const import (
    CONF_LOCAL_ENABLED,
    CONF_LOCAL_PASSWORD,
    DOMAIN,
)


async def test_local6004_reads_scenes(panel_config, local_password) -> None:
    """Read the static config straight off the panel over TCP 6004 (read-only)."""
    client = Local6004Client(panel_config["host"], str(local_password))
    cfg = await client.async_read_config()

    assert cfg.layout_ok, f"unexpected firmware layout: {cfg.firmware!r}"
    assert cfg.firmware.startswith("4."), cfg.firmware
    assert cfg.scenes, "expected at least one defined scenario"
    assert any(s.arms for s in cfg.scenes), "expected scenarios with partition targets"
    assert cfg.zone_areas, "expected zone->area mappings"

    print(
        f"\n[6004] firmware={cfg.firmware} scenes={len(cfg.scenes)} zone_areas={len(cfg.zone_areas)}"
    )
    for scene in cfg.scenes[:8]:
        print(f"  scene {scene.id}: {scene.arms}")


async def test_integration_creates_multiactive_scene_sensors(
    hass, panel_config, local_password
) -> None:
    """Set up the integration with 6004 enabled and assert scene sensors exist."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=panel_config,
        options={CONF_LOCAL_ENABLED: True, CONF_LOCAL_PASSWORD: str(local_password)},
        title="INIM PrimeX (E2E 6004)",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state.name == "LOADED"

    coordinator = entry.runtime_data.coordinator
    assert coordinator.local_config is not None
    assert coordinator.local_config.layout_ok
    assert entry.runtime_data.local_client is not None

    reg = er.async_get(hass)
    ents = er.async_entries_for_config_entry(reg, entry.entry_id)
    scene_sensors = [e for e in ents if "_scene_" in e.unique_id]
    assert scene_sensors, "expected multi-active scene binary sensors from 6004"

    active = sorted(coordinator.active_scene_ids())
    print(f"\n[6004] {len(scene_sensors)} scene sensors created; active scenarios now: {active}")
    # Each scene sensor's live state should agree with the computed active set.
    for ent in scene_sensors:
        state = hass.states.get(ent.entity_id)
        if state is not None and state.state in ("on", "off"):
            sid = int(ent.unique_id.rsplit("_", 1)[1])
            assert (state.state == "on") == (sid in active)
