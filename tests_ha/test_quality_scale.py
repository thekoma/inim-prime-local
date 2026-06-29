"""Quality-scale guardrail tests: parallel-updates, icons, entity translations."""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest
import yaml

from custom_components.inim_prime.client import FAULT_FLAG_KEYS

COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "inim_prime"


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("binary_sensor", 0),
        ("sensor", 0),
        ("alarm_control_panel", 1),
        ("switch", 1),
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
    "api_version",
    "api_connections",
    "api_last_ip",
}
_EXPECTED_BINARY_SENSOR_KEYS = {"system_fault", *FAULT_FLAG_KEYS}
_EXPECTED_EXCEPTION_KEYS = {
    "output_code_not_allowed",
    "zones_not_ready",
    "command_failed",
    "invalid_scenario",
    "forced_arm_bad_target",
    "forced_arm_single_target",
    "forced_arm_mode_with_scenario",
    "forced_arm_unknown_scenario",
    "forced_arm_area_or_scenario",
    "forced_arm_unbypassable_zones",
    "event_log_bad_target",
    "event_log_single_target",
}


@pytest.mark.parametrize("name", ["strings.json", "translations/en.json", "translations/it.json"])
def test_entity_translations_present(name: str) -> None:
    """Each fixed-name entity has a name under entity.<domain>."""
    data = _load(name)
    entity = data["entity"]
    assert set(entity["sensor"]) == _EXPECTED_SENSOR_KEYS
    assert set(entity["binary_sensor"]) == _EXPECTED_BINARY_SENSOR_KEYS
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
    for domain_block in icons.values():
        for entry in domain_block.values():
            assert entry["default"].startswith("mdi:")


# --- hassfest-class checks (run locally via pytest, so CI's hassfest job is
# rarely the first place a translation/services mistake is discovered) ---

_TRANSLATION_FILES = ("strings.json", "translations/en.json", "translations/it.json")
# A placeholder enclosed in single quotes (e.g. '{scenario}') — hassfest rejects
# it because ICU treats single-quoted text as a literal.
_PLACEHOLDER_IN_QUOTES = re.compile(r"'[^']*\{[^}]*\}[^']*'")


def _iter_strings(obj: object, path: str = ""):
    """Yield (dotted_path, value) for every string leaf in a nested mapping."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _iter_strings(value, f"{path}.{key}" if path else key)
    elif isinstance(obj, str):
        yield path, obj


def _key_paths(obj: object, path: str = "") -> set[str]:
    """Return the set of dotted key paths of a nested mapping."""
    keys: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else key
            keys.add(child)
            keys |= _key_paths(value, child)
    return keys


@pytest.mark.parametrize("name", _TRANSLATION_FILES)
def test_no_placeholder_inside_single_quotes(name: str) -> None:
    """No translatable string wraps a {placeholder} in single quotes."""
    for path, value in _iter_strings(_load(name)):
        assert not _PLACEHOLDER_IN_QUOTES.search(value), f"{name}: {path} -> {value!r}"


def test_translation_key_parity() -> None:
    """en/it translations mirror strings.json exactly (no missing/extra keys)."""
    base = _key_paths(_load("strings.json"))
    for name in ("translations/en.json", "translations/it.json"):
        assert _key_paths(_load(name)) == base, name


def test_services_yaml_matches_strings() -> None:
    """Every services.yaml action has a matching name + field keys in strings."""
    services = yaml.safe_load((COMPONENT_DIR / "services.yaml").read_text("utf-8"))
    strings_services = _load("strings.json").get("services", {})
    assert set(services) == set(strings_services)
    for service, spec in services.items():
        block = strings_services[service]
        assert "name" in block and "description" in block
        assert set((spec or {}).get("fields", {})) == set(block.get("fields", {}))


def test_manifest_keys_sorted() -> None:
    """manifest.json keys are domain, name, then alphabetical (hassfest rule)."""
    keys = list(_load("manifest.json"))
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])
