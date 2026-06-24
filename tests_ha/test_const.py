"""Tests for INIM Prime const helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custom_components.inim_prime.const import is_factory_default_area


@pytest.mark.parametrize(
    "label",
    [
        "AREA 006",
        "AREA       010",
        "AREA\t007",
        " AREA   006 ",  # surrounding whitespace is stripped
    ],
)
def test_is_factory_default_area_matches(label: str) -> None:
    """Uppercase factory-default area names are detected."""
    assert is_factory_default_area(label) is True


@pytest.mark.parametrize(
    "label",
    [
        "Area 5",  # manually set title-case -> used
        "Box",
        "Esterno",
        "Appartam.Giorno",
        "",
        "AREA",  # no number
        "AREA 006 extra",  # trailing text -> fullmatch fails
        "area 006",  # lowercase -> not the factory pattern
    ],
)
def test_is_factory_default_area_does_not_match(label: str) -> None:
    """Real / custom area names are not treated as factory-default."""
    assert is_factory_default_area(label) is False
