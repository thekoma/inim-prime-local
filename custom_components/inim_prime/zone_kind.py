"""Device class of a zone sensor: the panel configuration first, then the label.

Wired INIM zones have no door / window / motion field. What the configuration
read natively at setup does say for sure (see NativeZoneDef) wins over the
label: a fire zone, a roller-shutter or a shock detector (a shutter reads as an
opening: binary sensors have no shutter class, the entity gets shutter icons
instead, see binary_sensor.py). Then the label guess
(zone_guess.py). Weaker configuration hints are used only when the label says
nothing: a fault or hold-up zone reads as problem or safety, a 24-hour zone as
tamper, an interior zone as motion, a delayed or chime zone as a door.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass

from .client import NativeZoneDef
from .coordinator import InimDataUpdateCoordinator
from .zone_guess import guess_device_class


def native_zone(coordinator: InimDataUpdateCoordinator, zone_id: int) -> NativeZoneDef | None:
    """Return the zone's native definition, when the structure read provided it."""
    config = coordinator.local_config
    structure = config.structure if config is not None else None
    zones = structure.zones if structure is not None else None
    return next((zone for zone in zones or [] if zone.id == zone_id), None)


def zone_device_class(
    zone: NativeZoneDef | None, label: str, language: str | None = None
) -> BinarySensorDeviceClass | None:
    """Return the device class for a zone (see the module docstring for the order)."""
    if zone is not None:
        if zone.is_fire:
            return BinarySensorDeviceClass.SMOKE
        if zone.is_shutter:
            return BinarySensorDeviceClass.OPENING
        if zone.is_shock:
            return BinarySensorDeviceClass.VIBRATION
    guessed = guess_device_class(label, language)
    if guessed is not None or zone is None:
        return guessed
    if zone.is_fault_zone:
        return BinarySensorDeviceClass.PROBLEM
    if zone.is_hold_up:
        return BinarySensorDeviceClass.SAFETY
    if zone.is_24h:
        return BinarySensorDeviceClass.TAMPER
    if zone.is_interior:
        return BinarySensorDeviceClass.MOTION
    if zone.is_delayed or zone.has_chime:
        return BinarySensorDeviceClass.DOOR
    return None
