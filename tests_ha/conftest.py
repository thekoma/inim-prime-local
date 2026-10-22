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
    FAULT_FLAG_KEYS,
    AreaMode,
    Local6004Config,
    Local6004Structure,
    NativeAreaStatus,
    NativeGsmStatus,
    NativeObject,
    NativePowerValues,
    NativeSystemStatus,
    NativeZoneDef,
    NativeZoneStatus,
    SceneDef,
    ZoneState,
)
from custom_components.inim_prime.const import (  # noqa: E402
    CONF_LOCAL_PASSWORD,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom integration in every test."""
    yield


@pytest.fixture
def entry_data() -> dict:
    """Return valid config-entry data: the host and the panel LAN password."""
    return {"host": "192.0.2.10", CONF_LOCAL_PASSWORD: "pass"}


@pytest.fixture
def mock_config_entry(entry_data: dict) -> MockConfigEntry:
    """Return a mock (native-only, version 2) config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="INIM Prime",
        data=entry_data,
        unique_id="192.0.2.10",
        version=2,
    )


def system_status(
    *,
    supply: float = 13.7,
    service: bool = False,
    programming: bool = False,
    tamper_sources: dict[str, bool] | None = None,
    **faults: bool,
) -> NativeSystemStatus:
    """Build a system status with the given fault flags set (others clear)."""
    flags = {key: faults.get(key, False) for key in FAULT_FLAG_KEYS}
    return NativeSystemStatus(
        faults=flags,
        has_faults=any(flags.values()),
        supply_voltage=supply,
        battery_voltage=13.4,
        service=service,
        programming=programming,
        raw=bytes(12),
        tamper_sources=tamper_sources or {},
    )


@pytest.fixture
def make_system_status():
    """Return :func:`system_status`, to build a live system status in a test."""
    return system_status


@pytest.fixture
def mock_local_config() -> Local6004Config:
    """The panel's static config, as read natively at setup.

    One area (1, disarmed), one zone (1, in area 1), one scenario (1, whose
    definition disarms area 1, so it reads active) and one output (1).
    """
    return Local6004Config(
        firmware="4.07 PX020",
        layout_ok=True,
        scenes=[SceneDef(id=1, arms={1: "disarm"})],
        zone_areas={1: [1]},
        structure=Local6004Structure(
            areas=[NativeObject(1, "Home")],
            zones=[NativeZoneDef(1, "Front Door", 1, (1,))],
            scenarios=[NativeObject(1, "Away")],
            outputs=[NativeObject(1, "Siren")],
        ),
    )


@pytest.fixture
def area_statuses() -> dict[int, NativeAreaStatus]:
    """Live partition statuses: area 1 disarmed, no alarm."""
    return {1: NativeAreaStatus(mode=AreaMode.DISARMED, alarm=False, alarm_memory=False)}


@pytest.fixture
def zone_statuses() -> dict[int, NativeZoneStatus]:
    """Live zone statuses: zone 1 ready, not bypassed."""
    return {1: NativeZoneStatus(state=ZoneState.READY, excluded=False, alarm_memory=False)}


@pytest.fixture
def mock_local_client(
    mock_local_config: Local6004Config,
    area_statuses: dict[int, NativeAreaStatus],
    zone_statuses: dict[int, NativeZoneStatus],
) -> AsyncMock:
    """Return a stubbed Local6004Client answering with the fixtures above.

    The status readers return the fixture dicts themselves, so a test can
    change the live state by mutating them before the next refresh.
    """
    local = AsyncMock()
    local.async_read_config.return_value = mock_local_config
    local.async_get_area_statuses.return_value = area_statuses
    local.async_get_zone_statuses.return_value = zone_statuses
    local.async_get_output_states.return_value = {1: 0}
    local.async_get_system_status.return_value = system_status()
    local.async_get_power_values.return_value = NativePowerValues(
        battery_temperature=27, battery_resistance=115
    )
    local.async_get_gsm_status.return_value = NativeGsmStatus(
        result=0,
        faults={},
        networks=("2G", "3G", "4G"),
        signal=67,
        operator_code="00101",
    )
    local.supports_native_writes = True
    return local


@pytest.fixture
def patch_client(mock_local_client: AsyncMock):
    """Patch the native client in __init__ and config_flow; yield the mock."""
    with (
        patch(
            "custom_components.inim_prime.Local6004Client",
            return_value=mock_local_client,
        ),
        patch(
            "custom_components.inim_prime.config_flow.Local6004Client",
            return_value=mock_local_client,
        ),
    ):
        yield mock_local_client
