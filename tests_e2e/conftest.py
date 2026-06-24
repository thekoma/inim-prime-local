"""E2E fixtures: set up the real integration in HA core against the LIVE panel.

Requires homeassistant + pytest-homeassistant-custom-component and network access to
the panel. Reads connection details from environment (docker compose passes .env):
INIM_HOST, INIM_PORT, INIM_APIKEY, INIM_USE_HTTPS.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the repo root importable so HA's custom-integration loader can ``import custom_components``.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytest_plugins = ["pytest_homeassistant_custom_component"]

# Import the integration package so it is in sys.modules and HA's custom-component
# loader can discover it (mirrors tests_ha/conftest.py).
from custom_components.inim_prime.const import DOMAIN  # noqa: E402,F401

REQUIRED = ("INIM_HOST", "INIM_APIKEY")


class _Secret(str):
    """A str whose repr is redacted, so the apikey never lands in pytest output/logs."""

    def __repr__(self) -> str:  # noqa: D401
        return "'***'"


@pytest.fixture
def panel_config() -> dict:
    """Build config-entry data from env, or skip if the panel isn't configured."""
    if not all(os.environ.get(k) for k in REQUIRED):
        pytest.skip("INIM_HOST/INIM_APIKEY not set — skipping live E2E")
    return {
        "host": os.environ["INIM_HOST"],
        "port": int(os.environ.get("INIM_PORT", "8080")),
        "apikey": _Secret(os.environ["INIM_APIKEY"]),
        "use_https": os.environ.get("INIM_USE_HTTPS", "false").lower() == "true",
    }


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/inim_prime and allow real network to the live panel.

    pytest-homeassistant-custom-component blocks sockets and allowlists only 127.0.0.1;
    re-enable and allow the configured panel host for the E2E.
    """
    import pytest_socket

    pytest_socket.enable_socket()
    pytest_socket.socket_allow_hosts(
        [os.environ.get("INIM_HOST", "127.0.0.1"), "127.0.0.1"],
        allow_unix_socket=True,
    )
    yield
