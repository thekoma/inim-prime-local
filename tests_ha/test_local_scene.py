"""Tests for the read-only 6004 multi-active-scene integration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.binary_sensor import InimSceneActiveBinarySensor
from custom_components.inim_prime.client import (
    Area,
    AreaMode,
    AreaState,
    Fault,
    Local6004Config,
    Local6004Error,
    Scenario,
    SceneDef,
    Version,
    Zone,
    ZoneState,
)
from custom_components.inim_prime.const import (
    CONF_LOCAL_PASSWORD,
    DOMAIN,
)
from custom_components.inim_prime.coordinator import InimData, InimDataUpdateCoordinator


def _data(area_modes: dict[int, AreaMode]) -> InimData:
    return InimData(
        version=Version(version="4.07", verhttp="1.0", primex="4.07 PX020", servizio=False),
        areas=[
            Area(id=aid, label=f"Area {aid}", mode=mode, state=AreaState.READY, alarm_memory=False)
            for aid, mode in area_modes.items()
        ],
        zones=[
            Zone(
                id=1,
                label="Z",
                terminal=1,
                state=ZoneState.READY,
                alarm_memory=False,
                excluded=False,
            )
        ],
        scenarios=[Scenario(id=0, label="Ins.Box", active=False)],
        outputs=[],
        fault=Fault(vcc=13.7, raw_fau="0", has_faults=False),
        api_stats=None,
    )


def _fake_coord(area_modes, scenes, active_ids) -> SimpleNamespace:
    entry = SimpleNamespace(entry_id="abc", title="INIM Prime", options={})
    return SimpleNamespace(
        data=_data(area_modes),
        config_entry=entry,
        hass=SimpleNamespace(config=SimpleNamespace(language="it")),
        last_update_success=True,
        local_config=Local6004Config(firmware="4.07", layout_ok=True, scenes=scenes),
        active_scene_ids=lambda: set(active_ids),
    )


# ----------------------------------------------------------------- entity
def test_scene_sensor_on_off_and_metadata() -> None:
    scenes = [SceneDef(id=0, arms={3: "away"})]
    coord = _fake_coord({3: AreaMode.TOTAL}, scenes, active_ids={0})
    sensor = InimSceneActiveBinarySensor(coord, 0)
    assert sensor.unique_id == "abc_scene_0"
    assert sensor.name == "Ins.Box"  # live cgi label
    assert sensor.is_on is True
    # arms exposed by area label
    assert sensor.extra_state_attributes == {"arms": {"Area 3": "away"}}

    coord.active_scene_ids = lambda: set()
    assert sensor.is_on is False


def test_scene_sensor_label_fallback_when_no_cgi_match() -> None:
    coord = _fake_coord({}, [SceneDef(id=7, arms={0: "away"})], active_ids=set())
    sensor = InimSceneActiveBinarySensor(coord, 7)  # no cgi scenario with id 7
    assert sensor.name == "Scenario 7"


def test_scene_sensor_attributes_none_when_unknown() -> None:
    coord = _fake_coord({}, [SceneDef(id=0, arms={0: "away"})], active_ids=set())
    # a scene id with no matching definition -> no attributes
    sensor = InimSceneActiveBinarySensor(coord, 99)
    assert sensor.extra_state_attributes is None
    coord.local_config = None
    assert sensor.extra_state_attributes is None


# ----------------------------------------------------------- coordinator
async def test_active_scene_ids(hass: HomeAssistant, mock_config_entry, mock_client) -> None:
    coord = InimDataUpdateCoordinator(hass, mock_config_entry, mock_client)
    # no local config -> always empty
    coord.data = _data({0: AreaMode.TOTAL})
    assert coord.active_scene_ids() == set()
    # with definitions: scene 0 needs area0 away (matches), scene 1 needs disarm (no)
    coord.local_config = Local6004Config(
        firmware="4.07",
        layout_ok=True,
        scenes=[SceneDef(id=0, arms={0: "away"}), SceneDef(id=1, arms={0: "disarm"})],
    )
    assert coord.active_scene_ids() == {0}
    # no data at all -> empty
    coord.data = None
    assert coord.active_scene_ids() == set()


# --------------------------------------------------------------- setup (mandatory 6004)
async def test_setup_creates_scene_sensor(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """Mandatory 6004: setup reads the config and creates enabled scene sensors.

    The harness mock_local_config defines scene id 1 targeting area 1 (DISARMED),
    so the scene reads active.
    """
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coord = mock_config_entry.runtime_data.coordinator
    assert coord.local_config is not None
    assert mock_config_entry.runtime_data.local_client is not None
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{mock_config_entry.entry_id}_scene_1"
    )
    assert entity_id is not None
    # enabled by default (not disabled) and active
    assert registry.async_get(entity_id).disabled_by is None
    assert hass.states.get(entity_id).state == "on"


async def test_setup_missing_password_fails(
    hass: HomeAssistant, entry_data: dict, patch_client: AsyncMock
) -> None:
    """No LAN password -> setup errors (ConfigEntryError), nothing loads."""
    data = {k: v for k, v in entry_data.items() if k != CONF_LOCAL_PASSWORD}
    entry = MockConfigEntry(
        domain=DOMAIN, title="INIM Prime", data=data, unique_id="192.0.2.10:8080"
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_setup_local_read_error_not_ready(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """A 6004 read failure aborts setup (ConfigEntryNotReady) — no cgi-only fallback."""
    mock_config_entry.add_to_hass(hass)
    with patch("custom_components.inim_prime.Local6004Client") as cls:
        cls.return_value.async_read_config = AsyncMock(side_effect=Local6004Error("boom"))
        assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_wrong_firmware_errors(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, patch_client: AsyncMock
) -> None:
    """Unsupported (non-4.x) firmware aborts setup (ConfigEntryError)."""
    cfg = Local6004Config(firmware="3.10 PRIME", layout_ok=False)
    mock_config_entry.add_to_hass(hass)
    with patch("custom_components.inim_prime.Local6004Client") as cls:
        cls.return_value.async_read_config = AsyncMock(return_value=cfg)
        assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
