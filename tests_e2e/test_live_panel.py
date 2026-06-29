"""End-to-end: load the integration in HA core against the REAL panel and assert
that entities are created from live data. Read-only by default; Box arm/disarm is
gated behind INIM_E2E_ARM=1 (the user authorized arm/disarm tests on the Box area).
"""

import collections
import os
import re

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "inim_prime"
_FACTORY_AREA = re.compile(r"AREA\s+\d+")


async def _setup(hass, panel_config) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=panel_config, title="INIM PrimeX (E2E)")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_integration_loads_and_creates_entities(hass, panel_config):
    entry = await _setup(hass, panel_config)
    # entry loaded
    assert entry.state.recoverable is False or entry.state.name == "LOADED", entry.state
    assert entry.state.name == "LOADED"

    states = hass.states.async_all()
    alarms = [s for s in states if s.entity_id.startswith("alarm_control_panel.")]
    zones = [s for s in states if s.entity_id.startswith("binary_sensor.")]
    assert alarms, "expected at least one alarm_control_panel entity (areas)"
    assert zones, "expected at least one binary_sensor entity (zones/faults)"
    # a voltage sensor should exist with a real numeric value
    volts = [
        s
        for s in states
        if s.entity_id.startswith("sensor.") and s.attributes.get("unit_of_measurement") == "V"
    ]
    assert volts and float(volts[0].state) > 0, "expected a positive supply-voltage sensor"


async def test_declutter_hides_unused_areas_and_outputs(hass, panel_config):
    """Fresh setup against the live panel: factory-default areas (AREA NNN) and the
    output switches are registered but DISABLED by default; real areas and zone
    bypass switches stay enabled. Proves the declutter end-to-end."""
    entry = await _setup(hass, panel_config)
    reg = er.async_get(hass)
    ents = er.async_entries_for_config_entry(reg, entry.entry_id)
    disabled = lambda e: e.disabled_by is not None

    panels = [e for e in ents if e.domain == "alarm_control_panel"]
    factory = [
        e for e in panels if e.original_name and _FACTORY_AREA.fullmatch(e.original_name.strip())
    ]
    real = [e for e in panels if e not in factory]
    assert factory, "expected factory-default areas (AREA NNN) to be registered"
    assert all(disabled(e) for e in factory), "factory-default areas must be disabled-by-default"
    assert any(not disabled(e) for e in real), "real areas (Box/Esterno/...) must stay enabled"

    switches = [e for e in ents if e.domain == "switch"]
    outputs = [e for e in switches if e.original_name and not e.original_name.endswith("bypass")]
    bypass = [e for e in switches if e.original_name and e.original_name.endswith("bypass")]
    assert outputs and all(disabled(e) for e in outputs), (
        "output switches must be disabled-by-default"
    )
    assert bypass and any(not disabled(e) for e in bypass), "zone bypass switches must stay enabled"

    enabled = [e for e in ents if not disabled(e)]
    by_dom = collections.Counter(e.domain for e in enabled)
    print(
        f"\n[DECLUTTER] fresh setup: {len(enabled)}/{len(ents)} entities enabled "
        f"({len(ents) - len(enabled)} hidden). Enabled by domain: {dict(by_dom)}"
    )
    print(
        f"[DECLUTTER] hidden: {len(factory)} factory areas, {len(outputs)} output switches (+ their memory sensors/buttons)"
    )


@pytest.mark.skipif(
    os.environ.get("INIM_E2E_ARM") != "1", reason="set INIM_E2E_ARM=1 to run the Box arm/disarm E2E"
)
async def test_box_arm_disarm_roundtrip(hass, panel_config):
    """Arm then immediately disarm the 'Box' area via the alarm entity (authorized)."""
    await _setup(hass, panel_config)
    box = next(
        (
            s
            for s in hass.states.async_all()
            if s.entity_id.startswith("alarm_control_panel.")
            and "box" in (s.attributes.get("friendly_name", "").lower())
        ),
        None,
    )
    assert box is not None, "Box area alarm entity not found"

    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_arm_away",
        {"entity_id": box.entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()
    # restore immediately
    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_disarm",
        {"entity_id": box.entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()
    final = hass.states.get(box.entity_id)
    assert final.state in ("disarmed", "arming", "pending"), final.state
