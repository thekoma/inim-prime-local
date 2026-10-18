"""Tests for the per-label room override option (device.resolve_room)."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.inim_prime.const import CONF_ROOM_OVERRIDES, DOMAIN
from custom_components.inim_prime.device import (
    apply_room_overrides,
    parse_room_overrides,
    resolve_room,
    room_device_info,
)


def test_parse_room_overrides_skips_noise_and_folds() -> None:
    """Blank, comment and malformed lines are ignored; patterns are folded."""
    text = "\n".join(
        [
            "# a comment",
            "Taverna = Cameretta di Erik",
            "no-equals-here",
            "=only room",
            "only pattern =",
            "Guardaroba=Studio",
            "  PadWith   =   Spazi  ",
        ]
    )
    assert parse_room_overrides(text) == [
        ("taverna", "Cameretta di Erik"),
        ("guardaroba", "Studio"),
        ("padwith", "Spazi"),
    ]


def test_apply_room_overrides_first_match_wins() -> None:
    """The first matching rule wins, so a specific rule can precede a broad one."""
    overrides = parse_room_overrides(
        "\n".join(
            [
                "Volum.Taverna = Disimpegno",
                "Lucernaio = Soggiorno",
                "Sala = Cucina",
                "Taverna = Cameretta di Erik",
            ]
        )
    )
    assert apply_room_overrides("Volum.Taverna", overrides) == "Disimpegno"
    assert apply_room_overrides("Finestra Taverna", overrides) == "Cameretta di Erik"
    assert apply_room_overrides("Lucernaio Sala", overrides) == "Soggiorno"
    assert apply_room_overrides("Finestra Sala", overrides) == "Cucina"
    assert apply_room_overrides("Fin.Bagno PT", overrides) is None


def _coordinator(options: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        config_entry=SimpleNamespace(entry_id="abc", options=options),
        hass=SimpleNamespace(config=SimpleNamespace(language="it")),
    )


def test_resolve_room_override_then_guess_fallback() -> None:
    """An override wins; otherwise the label-based guess is used; else None."""
    coord = _coordinator({CONF_ROOM_OVERRIDES: "Guardaroba = Studio"})
    assert resolve_room(coord, "Fin.Guardaroba") == "Studio"  # override
    assert resolve_room(coord, "Finestra Camera") == "Camera"  # guess fallback
    assert resolve_room(coord, "AS Sirena Ext.") is None  # neither


def test_resolve_room_handles_non_string_option() -> None:
    """A non-string (e.g. unset) option value does not crash; guess still runs."""
    coord = _coordinator({CONF_ROOM_OVERRIDES: None})
    assert resolve_room(coord, "Finestra Camera") == "Camera"


def test_room_device_info_sets_suggested_area() -> None:
    """The per-room sub-device carries suggested_area so HA can auto-place it."""
    info = room_device_info(_coordinator({}), "Studio")
    assert info["suggested_area"] == "Studio"
    assert info["name"] == "Studio"
    assert (DOMAIN, "abc_room_studio") in info["identifiers"]
