"""Tests for the INIM Prime config flow (native only: host + LAN password)."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inim_prime.client import (
    AreaMode,
    Local6004Config,
    Local6004Error,
    Local6004Structure,
    NativeAreaStatus,
    NativeGsmStatus,
    NativeObject,
    NativePowerValues,
    SceneDef,
)
from custom_components.inim_prime.const import (
    CONF_GROUP_BY_ROOM,
    CONF_LABEL_LANGUAGE,
    CONF_LOCAL_PASSWORD,
    CONF_PANELS,
    CONF_REQUIRE_CODE,
    CONF_WEBHOOK_ENABLED,
    CONF_WEBHOOK_ID,
    DOMAIN,
)

USER_INPUT = {CONF_NAME: "Panel", CONF_HOST: "192.0.2.10", CONF_LOCAL_PASSWORD: "pass"}


async def _user_flow(hass: HomeAssistant, user_input: dict = USER_INPUT):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def test_user_flow_success(hass: HomeAssistant, patch_client: AsyncMock) -> None:
    """A readable panel creates an entry named after the friendly name."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert set(result["data_schema"].schema) == {CONF_NAME, CONF_HOST, CONF_LOCAL_PASSWORD}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Panel"
    assert result["data"] == USER_INPUT
    assert result["result"].unique_id == "192.0.2.10"
    assert result["result"].version == 2
    patch_client.async_read_config.assert_awaited()


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (Local6004Error("refused"), "cannot_connect"),
        (RuntimeError("unexpected"), "unknown"),
    ],
)
async def test_user_flow_errors_then_recovers(
    hass: HomeAssistant, patch_client: AsyncMock, failure: Exception, error: str
) -> None:
    """A read failure shows its error on the form, and a retry can succeed."""
    config = patch_client.async_read_config.return_value
    patch_client.async_read_config.side_effect = failure

    result = await _user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    patch_client.async_read_config.side_effect = None
    patch_client.async_read_config.return_value = config
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_unsupported_firmware(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """A panel whose firmware memory map is not known is refused."""
    patch_client.async_read_config.return_value = Local6004Config(
        firmware="6.00 PX020", layout_ok=False
    )

    result = await _user_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unsupported_firmware"}


async def test_user_flow_already_configured(hass: HomeAssistant, patch_client: AsyncMock) -> None:
    """The same host cannot be added twice."""
    MockConfigEntry(domain=DOMAIN, data=USER_INPUT, unique_id="192.0.2.10", version=2).add_to_hass(
        hass
    )

    result = await _user_flow(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    patch_client.async_read_config.assert_not_awaited()


def _add_entry(hass: HomeAssistant, **overrides) -> MockConfigEntry:
    """Register a config entry to reauthenticate or reconfigure."""
    data = {**USER_INPUT, **overrides}
    entry = MockConfigEntry(
        domain=DOMAIN, title="Panel", data=data, unique_id=data[CONF_HOST], version=2
    )
    entry.add_to_hass(hass)
    return entry


async def test_reauth_updates_the_password(hass: HomeAssistant, patch_client: AsyncMock) -> None:
    """Reauth asks only for the LAN password, validates it and stores it."""
    entry = _add_entry(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert set(result["data_schema"].schema) == {CONF_LOCAL_PASSWORD}
    assert result["description_placeholders"][CONF_HOST] == "192.0.2.10"

    patch_client.async_read_config.side_effect = Local6004Error("bad password")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCAL_PASSWORD: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data[CONF_LOCAL_PASSWORD] == "pass"

    patch_client.async_read_config.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCAL_PASSWORD: "rotated"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_LOCAL_PASSWORD] == "rotated"
    assert entry.data[CONF_HOST] == "192.0.2.10"


async def test_reconfigure_updates_name_host_and_password(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """Reconfigure moves the entry to a new host; the unique id follows."""
    entry = _add_entry(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert set(result["data_schema"].schema) == {CONF_NAME, CONF_HOST, CONF_LOCAL_PASSWORD}

    new_input = {CONF_NAME: "Alarm", CONF_HOST: "192.0.2.20", CONF_LOCAL_PASSWORD: "rotated"}
    patch_client.async_read_config.side_effect = Local6004Error("unreachable")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new_input)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    patch_client.async_read_config.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new_input)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.title == "Alarm"
    assert entry.data == new_input
    assert entry.unique_id == "192.0.2.20"


async def test_reconfigure_same_host_is_not_a_collision(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """Keeping the entry's own host is allowed."""
    entry = _add_entry(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_LOCAL_PASSWORD: "rotated"}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_LOCAL_PASSWORD] == "rotated"


async def test_reconfigure_onto_another_entry_aborts(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """Reconfiguring onto another entry's host aborts as already_configured."""
    entry = _add_entry(hass)
    _add_entry(hass, **{CONF_HOST: "192.0.2.99"})

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: "192.0.2.99"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    patch_client.async_read_config.assert_not_awaited()


async def test_reconfigure_unsupported_firmware(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """A host whose panel firmware is not supported is refused."""
    entry = _add_entry(hass)
    patch_client.async_read_config.return_value = replace(
        patch_client.async_read_config.return_value, layout_ok=False
    )

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["errors"] == {"base": "unsupported_firmware"}


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
async def _setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _open_settings(hass: HomeAssistant, entry) -> dict:  # noqa: ANN001
    """Open the options menu and pick the settings step, returning its form."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )


async def test_options_form_has_no_polling_fields(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """Only the areas, push mode, label language, room grouping and require-code."""
    entry = _add_entry(hass)
    await _setup_entry(hass, entry)

    result = await _open_settings(hass, entry)

    assert result["type"] is FlowResultType.FORM
    assert set(result["data_schema"].schema) == {
        "areas",
        CONF_WEBHOOK_ENABLED,
        CONF_LABEL_LANGUAGE,
        CONF_GROUP_BY_ROOM,
        CONF_REQUIRE_CODE,
    }


async def test_options_require_code_saved(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """Turning on 'require code' is stored in the entry options (off by default)."""
    entry = _add_entry(hass)
    await _setup_entry(hass, entry)
    assert entry.options.get(CONF_REQUIRE_CODE, False) is False

    result = await _open_settings(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_WEBHOOK_ENABLED: False,
            CONF_LABEL_LANGUAGE: "en",
            CONF_GROUP_BY_ROOM: True,
            CONF_REQUIRE_CODE: True,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_REQUIRE_CODE] is True


async def test_options_enable_push_generates_a_stable_webhook(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """Push mode stores a webhook id once, registers it and shows the URL."""
    entry = _add_entry(hass)
    await _setup_entry(hass, entry)

    result = await _open_settings(hass, entry)
    with patch("custom_components.inim_prime.config_flow.async_create_notification") as notify:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_WEBHOOK_ENABLED: True, CONF_LABEL_LANGUAGE: "en", CONF_GROUP_BY_ROOM: False},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    webhook_id = entry.options[CONF_WEBHOOK_ID]
    assert webhook_id
    assert entry.options[CONF_LABEL_LANGUAGE] == "en"
    assert entry.options[CONF_GROUP_BY_ROOM] is False
    assert webhook_id in notify.call_args.args[1]

    # Saving again keeps the same secret.
    result = await _open_settings(hass, entry)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_WEBHOOK_ENABLED: True}
    )
    await hass.async_block_till_done()
    assert entry.options[CONF_WEBHOOK_ID] == webhook_id


async def test_options_disable_push_drops_the_webhook_id(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """Turning push mode off removes the stored webhook id."""
    entry = _add_entry(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_WEBHOOK_ENABLED: True, CONF_WEBHOOK_ID: "secret"}
    )
    await _setup_entry(hass, entry)

    result = await _open_settings(hass, entry)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_WEBHOOK_ENABLED: False}
    )
    await hass.async_block_till_done()

    assert entry.options[CONF_WEBHOOK_ENABLED] is False
    assert CONF_WEBHOOK_ID not in entry.options


async def test_options_without_a_loaded_entry_offer_no_areas(
    hass: HomeAssistant, patch_client: AsyncMock
) -> None:
    """The area list comes from the running coordinator: none while not loaded."""
    entry = _add_entry(hass)

    result = await _open_settings(hass, entry)

    assert "areas" not in result["data_schema"].schema


# --------------------------------------------------------------------- panels
_PANEL_CONFIG = Local6004Config(
    firmware="4.07 PX500",
    layout_ok=True,
    scenes=[
        SceneDef(id=2, arms={0: "away", 1: "away"}),
        SceneDef(id=8, arms={0: "disarm", 1: "disarm"}),
    ],
    zone_areas={},
    structure=Local6004Structure(
        areas=[NativeObject(0, "Day"), NativeObject(1, "Night")],
        zones=[],
        scenarios=[NativeObject(2, "Arm all"), NativeObject(8, "Disarm all")],
        outputs=[],
    ),
)


@pytest.fixture
def patch_panel_client(make_system_status):  # noqa: ANN001, ANN201
    """Patch the client with two areas and an inverse scenario pair."""
    client = AsyncMock()
    client.async_read_config.return_value = _PANEL_CONFIG
    client.async_get_area_statuses.return_value = {
        0: NativeAreaStatus(mode=AreaMode.DISARMED, alarm=False, alarm_memory=False),
        1: NativeAreaStatus(mode=AreaMode.DISARMED, alarm=False, alarm_memory=False),
    }
    client.async_get_zone_statuses.return_value = {}
    client.async_get_output_states.return_value = {}
    client.async_get_system_status.return_value = make_system_status()
    client.async_get_power_values.return_value = NativePowerValues(
        battery_temperature=27, battery_resistance=115
    )
    client.async_get_gsm_status.return_value = NativeGsmStatus(
        result=0, faults={}, networks=("4G",), signal=67, operator_code="00101"
    )
    client.supports_native_writes = True
    with (
        patch("custom_components.inim_prime.Local6004Client", return_value=client),
        patch("custom_components.inim_prime.config_flow.Local6004Client", return_value=client),
    ):
        yield client


async def _open_panels(hass: HomeAssistant, entry) -> dict:  # noqa: ANN001
    """Open the options menu and step into the panels menu."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "panels"}
    )


async def _pick(hass: HomeAssistant, result: dict, step: str) -> dict:
    """Pick a menu step from a menu result."""
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


async def test_panels_menu_lists_the_available_actions(
    hass: HomeAssistant, patch_panel_client: AsyncMock
) -> None:
    """With an inverse pair and scenarios present, the menu offers every add action."""
    entry = _add_entry(hass)
    await _setup_entry(hass, entry)

    result = await _open_panels(hass, entry)

    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {
        "panel_add_inverse",
        "panel_add_areas",
        "panel_add_scenarios",
    }


async def test_panel_add_areas_creates_a_panel(
    hass: HomeAssistant, patch_panel_client: AsyncMock
) -> None:
    """Adding an areas panel stores a name-agnostic definition."""
    entry = _add_entry(hass)
    await _setup_entry(hass, entry)

    result = await _pick(hass, await _open_panels(hass, entry), "panel_add_areas")
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "House", "areas": ["0", "1"]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    panels = entry.options[CONF_PANELS]
    assert len(panels) == 1
    assert panels[0]["kind"] == "areas"
    assert panels[0]["name"] == "House"
    assert panels[0]["areas"] == [0, 1]


async def test_panel_add_scenarios_creates_a_panel(
    hass: HomeAssistant, patch_panel_client: AsyncMock
) -> None:
    """Adding a scenarios panel stores the arm/disarm scenario ids."""
    entry = _add_entry(hass)
    await _setup_entry(hass, entry)

    result = await _pick(hass, await _open_panels(hass, entry), "panel_add_scenarios")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"name": "All", "arm_scenario": "2", "disarm_scenario": "8"},
    )
    await hass.async_block_till_done()

    panels = entry.options[CONF_PANELS]
    assert panels[0]["kind"] == "scenarios"
    assert panels[0]["arm_scenario"] == 2
    assert panels[0]["disarm_scenario"] == 8


async def test_panel_add_inverse_creates_from_selected_pairs(
    hass: HomeAssistant, patch_panel_client: AsyncMock
) -> None:
    """Picking a detected pair creates a scenarios panel named after the arm scene."""
    entry = _add_entry(hass)
    await _setup_entry(hass, entry)

    result = await _pick(hass, await _open_panels(hass, entry), "panel_add_inverse")
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"pairs": ["2:8"]}
    )
    await hass.async_block_till_done()

    panels = entry.options[CONF_PANELS]
    assert len(panels) == 1
    assert panels[0]["kind"] == "scenarios"
    assert panels[0]["name"] == "Arm all"
    assert panels[0]["arm_scenario"] == 2
    assert panels[0]["disarm_scenario"] == 8


async def test_panel_remove(hass: HomeAssistant, patch_panel_client: AsyncMock) -> None:
    """An existing panel can be removed from the menu."""
    entry = _add_entry(
        hass,
    )
    hass.config_entries.async_update_entry(
        entry,
        options={
            CONF_PANELS: [{"id": "keep", "kind": "areas", "name": "A", "areas": [0]},
                          {"id": "drop", "kind": "areas", "name": "B", "areas": [1]}]
        },
    )
    await _setup_entry(hass, entry)

    result = await _pick(hass, await _open_panels(hass, entry), "panel_remove")
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"remove": ["drop"]}
    )
    await hass.async_block_till_done()

    ids = [p["id"] for p in entry.options[CONF_PANELS]]
    assert ids == ["keep"]


async def test_panel_add_areas_aborts_without_a_loaded_entry(
    hass: HomeAssistant, patch_panel_client: AsyncMock
) -> None:
    """With no running coordinator there are no areas, so the step aborts."""
    entry = _add_entry(hass)  # added but not set up

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await _pick(hass, result, "panels")
    result = await _pick(hass, result, "panel_add_areas")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_areas"


async def test_panel_entity_follows_the_live_area_state(
    hass: HomeAssistant, patch_panel_client: AsyncMock
) -> None:
    """A configured panel entity appears and tracks the member areas' live state."""
    from homeassistant.helpers import entity_registry as er

    entry = _add_entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        options={CONF_PANELS: [{"id": "p1", "kind": "areas", "name": "House", "areas": [0, 1]}]},
    )
    await _setup_entry(hass, entry)

    entity_id = er.async_get(hass).async_get_entity_id(
        "alarm_control_panel", DOMAIN, f"{entry.entry_id}_panel_p1"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "disarmed"

    statuses = patch_panel_client.async_get_area_statuses.return_value
    statuses[0] = NativeAreaStatus(mode=AreaMode.TOTAL, alarm=False, alarm_memory=False)
    statuses[1] = NativeAreaStatus(mode=AreaMode.TOTAL, alarm=False, alarm_memory=False)
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "armed_away"


async def test_panel_edit_renames_and_changes_areas(
    hass: HomeAssistant, patch_panel_client: AsyncMock
) -> None:
    """Editing an areas panel renames it and changes its members, keeping its id."""
    entry = _add_entry(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_PANELS: [{"id": "p1", "kind": "areas", "name": "Old", "areas": [0]}]}
    )
    await _setup_entry(hass, entry)

    result = await _pick(hass, await _open_panels(hass, entry), "panel_edit")
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"panel": "p1"})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Casa", "areas": ["0", "1"]}
    )
    await hass.async_block_till_done()

    panels = entry.options[CONF_PANELS]
    assert len(panels) == 1
    assert panels[0]["id"] == "p1"
    assert panels[0]["name"] == "Casa"
    assert panels[0]["areas"] == [0, 1]


async def test_panel_edit_scenarios(
    hass: HomeAssistant, patch_panel_client: AsyncMock
) -> None:
    """Editing a scenarios panel renames it and swaps its arm/disarm scenes."""
    entry = _add_entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        options={
            CONF_PANELS: [
                {"id": "s1", "kind": "scenarios", "name": "Old", "arm_scenario": 2,
                 "disarm_scenario": 8}
            ]
        },
    )
    await _setup_entry(hass, entry)

    result = await _pick(hass, await _open_panels(hass, entry), "panel_edit")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"panel": "s1"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Tutto", "arm_scenario": "8", "disarm_scenario": "2"}
    )
    await hass.async_block_till_done()

    p = entry.options[CONF_PANELS][0]
    assert p == {"id": "s1", "kind": "scenarios", "name": "Tutto", "arm_scenario": 8,
                 "disarm_scenario": 2}
