"""Quality-scale guardrail tests: parallel-updates, icons, entity translations."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from custom_components.inim_prime.client import FAULT_FLAG_KEYS

COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "inim_prime"


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("binary_sensor", 0),
        ("sensor", 0),
        ("alarm_control_panel", 1),
        ("switch", 1),
        ("select", 1),
        ("button", 1),
    ],
)
def test_parallel_updates_present(platform: str, expected: int) -> None:
    """Every platform declares PARALLEL_UPDATES with the documented value."""
    module = importlib.import_module(f"custom_components.inim_prime.{platform}")
    assert hasattr(module, "PARALLEL_UPDATES"), platform
    assert module.PARALLEL_UPDATES == expected


def _load(name: str) -> dict:
    return json.loads((COMPONENT_DIR / name).read_text(encoding="utf-8"))


def test_json_files_valid() -> None:
    """strings.json, translations and icons.json parse as JSON."""
    for name in (
        "strings.json",
        "icons.json",
        "translations/en.json",
        "translations/it.json",
    ):
        _load(name)


def test_strings_en_identical() -> None:
    """The English translation mirrors strings.json exactly."""
    assert _load("strings.json") == _load("translations/en.json")


# Expected translated entity keys per platform/domain.
_EXPECTED_SENSOR_KEYS = {
    "supply_voltage",
    "open_zone_count",
    "active_scenario",
    "api_connections",
    "api_last_ip",
}
_EXPECTED_BINARY_SENSOR_KEYS = {"system_fault", *FAULT_FLAG_KEYS}
_EXPECTED_EXCEPTION_KEYS = {
    "output_code_not_allowed",
    "zones_not_ready",
    "command_failed",
    "invalid_scenario",
}


@pytest.mark.parametrize("name", ["strings.json", "translations/en.json", "translations/it.json"])
def test_entity_translations_present(name: str) -> None:
    """Each fixed-name entity has a name under entity.<domain>."""
    data = _load(name)
    entity = data["entity"]
    assert set(entity["sensor"]) == _EXPECTED_SENSOR_KEYS
    assert set(entity["binary_sensor"]) == _EXPECTED_BINARY_SENSOR_KEYS
    assert set(entity["select"]) == {"active_scenario"}
    for domain_block in entity.values():
        for entry in domain_block.values():
            assert entry["name"]


@pytest.mark.parametrize("name", ["strings.json", "translations/en.json", "translations/it.json"])
def test_exception_translations_present(name: str) -> None:
    """Every action-exception translation key is defined with a message."""
    data = _load(name)
    exceptions = data["exceptions"]
    assert set(exceptions) == _EXPECTED_EXCEPTION_KEYS
    assert "{error}" in exceptions["command_failed"]["message"]


def test_icons_match_translated_entities() -> None:
    """icons.json covers exactly the translated entities, with mdi: defaults."""
    icons = _load("icons.json")["entity"]
    assert set(icons["sensor"]) == _EXPECTED_SENSOR_KEYS
    assert set(icons["binary_sensor"]) == _EXPECTED_BINARY_SENSOR_KEYS
    assert set(icons["select"]) == {"active_scenario"}
    for domain_block in icons.values():
        for entry in domain_block.values():
            assert entry["default"].startswith("mdi:")
