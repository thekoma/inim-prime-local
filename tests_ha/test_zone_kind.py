"""Tests for the zone device class from the panel configuration (zone_kind.py)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import Local6004Config, Local6004Structure, NativeZoneDef
from custom_components.inim_prime.const import DOMAIN
from custom_components.inim_prime.zone_kind import native_zone, zone_device_class

_PLAIN = NativeZoneDef(7, "Zone 7", 7, (0,), zone_type=0, options=0, options1=0, options2=0, sensor=0)


@pytest.mark.parametrize(
    ("changes", "label", "expected"),
    [
        # Explicit configuration wins over the label.
        ({"options2": 0x04}, "Front Door", BinarySensorDeviceClass.SMOKE),
        ({"sensor": 1}, "Garage", BinarySensorDeviceClass.OPENING),
        ({"sensor": 2}, "Front Door", BinarySensorDeviceClass.VIBRATION),
        # Otherwise the label decides.
        ({}, "Front Door", BinarySensorDeviceClass.DOOR),
        ({"options": 0x01}, "Kitchen window", BinarySensorDeviceClass.WINDOW),
        # Weak hints only when the label says nothing.
        ({"options1": 0x40}, "Zone 7", BinarySensorDeviceClass.PROBLEM),
        ({"options1": 0x20}, "Zone 7", BinarySensorDeviceClass.SAFETY),
        ({"zone_type": 4}, "Zone 7", BinarySensorDeviceClass.TAMPER),
        ({"options": 0x01}, "Zone 7", BinarySensorDeviceClass.MOTION),
        ({"zone_type": 1}, "Zone 7", BinarySensorDeviceClass.DOOR),
        ({"zone_type": 2}, "Zone 7", BinarySensorDeviceClass.DOOR),
        ({"options": 0x08}, "Zone 7", BinarySensorDeviceClass.DOOR),
        ({}, "Zone 7", None),
    ],
)
def test_zone_device_class(changes: dict, label: str, expected) -> None:  # noqa: ANN001
    assert zone_device_class(replace(_PLAIN, **changes), label, "en") is expected


def test_zone_device_class_without_a_native_definition() -> None:
    """No native definition: the label alone decides, and may say nothing."""
    assert zone_device_class(None, "Front Door", "en") is BinarySensorDeviceClass.DOOR
    assert zone_device_class(None, "Zone 7", "en") is None


def test_unread_kind_fields_are_not_hints() -> None:
    """A zone whose kind tables were not read gets no configuration hint."""
    bare = NativeZoneDef(7, "Zone 7", 7, (0,))
    assert zone_device_class(bare, "Zone 7", "en") is None


@pytest.mark.parametrize(
    "local_config",
    [
        None,
        SimpleNamespace(structure=None),
        SimpleNamespace(structure=Local6004Structure(zones=None)),
        SimpleNamespace(structure=Local6004Structure(zones=[_PLAIN])),
    ],
)
def test_native_zone_missing(local_config) -> None:  # noqa: ANN001
    coordinator = SimpleNamespace(local_config=local_config)
    assert native_zone(coordinator, 3) is None  # type: ignore[arg-type]


def test_native_zone_found() -> None:
    coordinator = SimpleNamespace(
        local_config=SimpleNamespace(structure=Local6004Structure(zones=[_PLAIN]))
    )
    assert native_zone(coordinator, 7) is _PLAIN  # type: ignore[arg-type]


@pytest.fixture
def mock_local_config(mock_local_config: Local6004Config) -> Local6004Config:
    """The sample zone ("Front Door") is wired as a roller-shutter detector."""
    structure = mock_local_config.structure
    assert structure is not None and structure.zones is not None
    shutter = replace(structure.zones[0], sensor=1)
    return replace(mock_local_config, structure=replace(structure, zones=[shutter]))


async def test_zone_sensor_uses_the_configuration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """End to end: the detector type beats the label for the entity's device class."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{mock_config_entry.entry_id}_zone_1"
    )
    assert entity_id is not None
    # No shutter class for binary sensors: "opening", with the shutter icons.
    assert hass.states.get(entity_id).attributes["device_class"] == "opening"
    assert registry.entities[entity_id].translation_key == "shutter"
    # The entity keeps the zone label as its name.
    assert hass.states.get(entity_id).name.endswith("Front Door")
