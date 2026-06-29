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

from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.inim_prime.client import (  # noqa: E402
    ApiStats,
    Area,
    AreaMode,
    AreaState,
    Fault,
    Local6004Config,
    Output,
    Scenario,
    SceneDef,
    Version,
    Zone,
    ZoneState,
)
from custom_components.inim_prime.const import (  # noqa: E402
    CONF_APIKEY,
    CONF_LOCAL_PASSWORD,
    CONF_USE_HTTPS,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom integration in every test."""
    yield


@pytest.fixture
def entry_data() -> dict:
    """Return valid config-entry data (the 6004 LAN password is mandatory)."""
    return {
        "host": "192.0.2.10",
        "port": 8080,
        CONF_APIKEY: "secret-key",
        CONF_USE_HTTPS: False,
        CONF_LOCAL_PASSWORD: "pass",
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
        # ``version`` is the local API version (NOT the firmware); the real
        # firmware (4.07) lives in ``primex`` — mirrors the live panel response.
        version="1.0.1",
        verhttp="1.0.0",
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
def mock_local_config() -> Local6004Config:
    """A valid 6004 config (mandatory channel) used by the test harness.

    Scene 1 targets area 1 (sample_areas area 1 is DISARMED) so it reads active.
    """
    return Local6004Config(
        firmware="4.07 PX020",
        layout_ok=True,
        scenes=[SceneDef(id=1, arms={1: "disarm"})],
        zone_areas={1: [1]},
    )


@pytest.fixture
def patch_client(mock_client: AsyncMock, mock_local_config: Local6004Config):
    """Patch the cgi client AND the mandatory 6004 client in __init__/config_flow."""
    local = AsyncMock()
    local.async_read_config.return_value = mock_local_config
    with (
        patch(
            "custom_components.inim_prime.InimPrimeClient",
            return_value=mock_client,
        ),
        patch(
            "custom_components.inim_prime.config_flow.InimPrimeClient",
            return_value=mock_client,
        ),
        patch(
            "custom_components.inim_prime.Local6004Client",
            return_value=local,
        ),
        patch(
            "custom_components.inim_prime.config_flow.Local6004Client",
            return_value=local,
        ),
    ):
        yield mock_client
