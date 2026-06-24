"""Fixtures for the INIM Prime HA integration tests."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Ensure the repo root (which contains ``custom_components``) is importable so
# that the HA custom-integration loader can ``import custom_components``.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custom_components.inim_prime.client import (  # noqa: E402
    ApiStats,
    Area,
    AreaMode,
    AreaState,
    Fault,
    Output,
    Scenario,
    Version,
    Zone,
    ZoneState,
)

from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.inim_prime.const import (  # noqa: E402
    CONF_APIKEY,
    CONF_USE_HTTPS,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom integration in every test."""
    yield


@pytest.fixture
def entry_data() -> dict:
    """Return valid config-entry data."""
    return {
        "host": "192.0.2.10",
        "port": 8080,
        CONF_APIKEY: "secret-key",
        CONF_USE_HTTPS: False,
        "scan_interval": 15,
    }


@pytest.fixture
def mock_config_entry(entry_data: dict) -> MockConfigEntry:
    """Return a mock config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="INIM Prime",
        data=entry_data,
        unique_id="192.0.2.10:8080",
    )


@pytest.fixture
def sample_version() -> Version:
    """Return a sample Version."""
    return Version(
        version="4.07",
        verhttp="1.0",
        primex="4.07 PX500",
        servizio=False,
    )


@pytest.fixture
def sample_areas() -> list[Area]:
    """Return sample areas."""
    return [
        Area(
            id=1,
            label="Home",
            mode=AreaMode.DISARMED,
            state=AreaState.READY,
            alarm_memory=False,
        )
    ]


@pytest.fixture
def sample_zones() -> list[Zone]:
    """Return sample zones."""
    return [
        Zone(
            id=1,
            label="Front Door",
            terminal=1,
            state=ZoneState.READY,
            alarm_memory=False,
            excluded=False,
        )
    ]


@pytest.fixture
def sample_scenarios() -> list[Scenario]:
    """Return sample scenarios."""
    return [Scenario(id=1, label="Away", active=False)]


@pytest.fixture
def sample_outputs() -> list[Output]:
    """Return sample outputs."""
    return [Output(id=1, label="Siren", terminal=1, state=0, type=0)]


@pytest.fixture
def sample_fault() -> Fault:
    """Return a sample Fault."""
    return Fault(vcc=13.7, raw_fau="0", has_faults=False)


@pytest.fixture
def sample_api_stats() -> ApiStats:
    """Return sample ApiStats."""
    return ApiStats(
        api="primex",
        connections=3,
        last_connection="12:00 24/06/2026",
        last_ip="192.0.2.1",
    )


@pytest.fixture
def mock_client(
    sample_version: Version,
    sample_areas: list[Area],
    sample_zones: list[Zone],
    sample_scenarios: list[Scenario],
    sample_outputs: list[Output],
    sample_fault: Fault,
    sample_api_stats: ApiStats,
) -> AsyncMock:
    """Return a fully-stubbed InimPrimeClient."""
    client = AsyncMock()
    client.version.return_value = sample_version
    client.get_areas.return_value = sample_areas
    client.get_zones.return_value = sample_zones
    client.get_scenarios.return_value = sample_scenarios
    client.get_outputs.return_value = sample_outputs
    client.get_faults.return_value = sample_fault
    client.get_api_stats.return_value = sample_api_stats
    return client


@pytest.fixture
def patch_client(mock_client: AsyncMock):
    """Patch the client constructor in both __init__ and config_flow."""
    with (
        patch(
            "custom_components.inim_prime.InimPrimeClient",
            return_value=mock_client,
        ),
        patch(
            "custom_components.inim_prime.config_flow.InimPrimeClient",
            return_value=mock_client,
        ),
    ):
        yield mock_client
