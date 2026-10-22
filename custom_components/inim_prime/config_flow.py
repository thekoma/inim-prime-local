"""Config flow for the INIM Prime integration (native TCP 6004 only)."""

from __future__ import annotations

import uuid
from typing import Any

import voluptuous as vol
from homeassistant.components import webhook
from homeassistant.components.persistent_notification import (
    async_create as async_create_notification,
)
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .areas import default_area_selection
from .client import Area, Local6004Client, Local6004Error, Scenario, SceneDef
from .const import (
    CONF_AREAS,
    CONF_AREAS_OFFERED,
    CONF_GROUP_BY_ROOM,
    CONF_LABEL_LANGUAGE,
    CONF_LOCAL_PASSWORD,
    CONF_PANELS,
    CONF_REQUIRE_CODE,
    CONF_WEBHOOK_ENABLED,
    CONF_WEBHOOK_ID,
    DEFAULT_GROUP_BY_ROOM,
    DEFAULT_LOCAL_PASSWORD,
    DEFAULT_NAME,
    DEFAULT_REQUIRE_CODE,
    DOMAIN,
    LABEL_LANGUAGE_AUTO,
    LOCAL_6004_PORT,
)
from .coordinator import InimConfigEntry
from .panels import PanelDef, PanelKind, inverse_scenario_pairs, load_panels
from .zone_guess import SUPPORTED_LANGUAGES

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class InimPrimeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for INIM Prime.

    Version 2: native only (host + LAN password). Version 1 entries also held
    the panel's cgi settings; ``async_migrate_entry`` drops them.
    """

    VERSION = 2

    async def _async_validate(self, host: str, password: str) -> dict[str, str]:
        """Read the panel's config natively. Return a (possibly empty) error map."""
        client = Local6004Client(host=host, password=password, port=LOCAL_6004_PORT)
        try:
            config = await client.async_read_config()
        except Local6004Error:
            return {"base": "cannot_connect"}
        except Exception:  # noqa: BLE001
            return {"base": "unknown"}
        if not config.layout_ok:
            return {"base": "unsupported_firmware"}
        return {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

            errors = await self._async_validate(host, user_input[CONF_LOCAL_PASSWORD])
            if not errors:
                return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_LOCAL_PASSWORD, default=DEFAULT_LOCAL_PASSWORD): _PASSWORD,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle reauthentication: the panel LAN password changed."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauthentication by re-entering the LAN password.

        The host is taken from the existing entry (not changeable here); only
        the password is re-entered and validated against the panel.
        """
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            password = user_input[CONF_LOCAL_PASSWORD]
            errors = await self._async_validate(entry.data[CONF_HOST], password)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_LOCAL_PASSWORD: password}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_LOCAL_PASSWORD): _PASSWORD}),
            description_placeholders={CONF_HOST: entry.data[CONF_HOST]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure an existing entry (name, host or LAN password)."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            host = user_input[CONF_HOST]
            # unique_id is the host. Allow it to change as part of the
            # reconfigure, but reject collisions with a *different* entry.
            collision = self.hass.config_entries.async_entry_for_domain_unique_id(
                self.handler, host
            )
            if collision is not None and collision.entry_id != entry.entry_id:
                return self.async_abort(reason="already_configured")

            errors = await self._async_validate(host, user_input[CONF_LOCAL_PASSWORD])
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    title=user_input[CONF_NAME],
                    unique_id=host,
                    data_updates=user_input,
                )

        suggested = {**entry.data, **(user_input or {})}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=suggested.get(CONF_NAME, entry.title)): str,
                    vol.Required(CONF_HOST, default=suggested.get(CONF_HOST)): str,
                    vol.Required(
                        CONF_LOCAL_PASSWORD,
                        default=suggested.get(CONF_LOCAL_PASSWORD, DEFAULT_LOCAL_PASSWORD),
                    ): _PASSWORD,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: InimConfigEntry,
    ) -> InimPrimeOptionsFlow:
        """Create the options flow."""
        return InimPrimeOptionsFlow()


class InimPrimeOptionsFlow(OptionsFlowWithReload):
    """Handle INIM Prime options.

    The areas to show, the zone-label language and room grouping, and a "push
    mode" toggle. Enabling push mode generates a per-entry webhook id (the
    cleartext-LAN shared secret) and surfaces the full local webhook URL to the
    user via a persistent notification so they can paste it into PrimeStudio.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options menu: the settings form, or panel management."""
        return self.async_show_menu(step_id="init", menu_options=["settings", "panels"])

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The settings form: areas, push mode, label language, grouping, code."""
        options = dict(self.config_entry.options)
        areas = _panel_areas(self.config_entry)

        if user_input is not None:
            new_options = {**options, **user_input}
            if CONF_AREAS in user_input:
                # Remember what was offered, so an area added on the panel later
                # is told apart from one deselected here (see areas.py).
                new_options[CONF_AREAS] = sorted(user_input[CONF_AREAS], key=int)
                new_options[CONF_AREAS_OFFERED] = [str(area.id) for area in areas]

            if user_input.get(CONF_WEBHOOK_ENABLED):
                # Generate the secret once and keep it stable across edits.
                webhook_id = options.get(CONF_WEBHOOK_ID) or webhook.async_generate_id()
                new_options[CONF_WEBHOOK_ID] = webhook_id
                url = webhook.async_generate_url(self.hass, webhook_id)
                async_create_notification(
                    self.hass,
                    (
                        "INIM Prime realtime push is enabled. Configure your "
                        "panel (PrimeStudio) to call this local webhook URL:\n\n"
                        f"`{url}`\n\n"
                        "Append event params, e.g. `?ev=zone_open&id=12`. "
                        "Treat this URL as a secret (cleartext on the LAN)."
                    ),
                    title="INIM Prime webhook URL",
                    notification_id=f"{DOMAIN}_webhook_{self.config_entry.entry_id}",
                )
            else:
                new_options.pop(CONF_WEBHOOK_ID, None)

            return self.async_create_entry(data=new_options)

        schema: dict[vol.Marker, Any] = {}
        if areas:
            # Offered only while the entry is loaded: the area list comes from
            # the running coordinator. Ids not on the panel any more are dropped.
            offered = {str(area.id) for area in areas}
            selected = options.get(
                CONF_AREAS, default_area_selection(self.hass, self.config_entry, areas)
            )
            schema[
                vol.Optional(CONF_AREAS, default=[key for key in selected if key in offered])
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value=str(area.id),
                            label=" ".join(area.label.split()) or str(area.id + 1),
                        )
                        for area in areas
                    ],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )

        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    **schema,
                    vol.Required(
                        CONF_WEBHOOK_ENABLED,
                        default=options.get(CONF_WEBHOOK_ENABLED, False),
                    ): bool,
                    vol.Required(
                        CONF_LABEL_LANGUAGE,
                        default=options.get(CONF_LABEL_LANGUAGE, LABEL_LANGUAGE_AUTO),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[LABEL_LANGUAGE_AUTO, *SUPPORTED_LANGUAGES],
                            translation_key="label_language",
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_GROUP_BY_ROOM,
                        default=options.get(CONF_GROUP_BY_ROOM, DEFAULT_GROUP_BY_ROOM),
                    ): bool,
                    vol.Required(
                        CONF_REQUIRE_CODE,
                        default=options.get(CONF_REQUIRE_CODE, DEFAULT_REQUIRE_CODE),
                    ): bool,
                }
            ),
        )

    # ------------------------------------------------------------------ panels
    def _current_panels(self) -> list[PanelDef]:
        """The panels currently stored for this entry."""
        return load_panels(self.config_entry.options.get(CONF_PANELS))

    def _save_panels(self, panels: list[PanelDef]) -> ConfigFlowResult:
        """Persist the panel list (reloads the entry via OptionsFlowWithReload)."""
        new_options = {
            **self.config_entry.options,
            CONF_PANELS: [panel.to_dict() for panel in panels],
        }
        return self.async_create_entry(data=new_options)

    async def async_step_panels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Panel-management menu, with only the actions that apply."""
        menu: list[str] = []
        if inverse_scenario_pairs(_scenes(self.config_entry)):
            menu.append("panel_add_inverse")
        menu.append("panel_add_areas")
        if _scenarios(self.config_entry):
            menu.append("panel_add_scenarios")
        if self._current_panels():
            menu.append("panel_remove")
        return self.async_show_menu(step_id="panels", menu_options=menu)

    async def async_step_panel_add_areas(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create an areas panel from a chosen set of partitions."""
        areas = _panel_areas(self.config_entry)
        if not areas:
            return self.async_abort(reason="no_areas")
        if user_input is not None:
            panels = self._current_panels()
            panels.append(
                PanelDef(
                    id=uuid.uuid4().hex[:8],
                    kind=PanelKind.AREAS,
                    name=user_input["name"],
                    areas=tuple(int(a) for a in user_input["areas"]),
                )
            )
            return self._save_panels(panels)
        return self.async_show_form(
            step_id="panel_add_areas",
            data_schema=vol.Schema(
                {
                    vol.Required("name"): str,
                    vol.Required("areas"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=str(area.id),
                                    label=" ".join(area.label.split()) or str(area.id + 1),
                                )
                                for area in areas
                            ],
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    async def async_step_panel_add_scenarios(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create a panel from an arm scenario plus a disarm scenario.

        Only reachable from the panels menu, which lists this action only while
        the panel has scenarios, so the lists below are never empty.
        """
        scenarios = _scenarios(self.config_entry)
        if user_input is not None:
            panels = self._current_panels()
            panels.append(
                PanelDef(
                    id=uuid.uuid4().hex[:8],
                    kind=PanelKind.SCENARIOS,
                    name=user_input["name"],
                    arm_scenario=int(user_input["arm_scenario"]),
                    disarm_scenario=int(user_input["disarm_scenario"]),
                )
            )
            return self._save_panels(panels)
        choices = [
            SelectOptionDict(value=str(s.id), label=" ".join(s.label.split()) or str(s.id))
            for s in scenarios
        ]
        selector = SelectSelector(
            SelectSelectorConfig(options=choices, mode=SelectSelectorMode.DROPDOWN)
        )
        return self.async_show_form(
            step_id="panel_add_scenarios",
            data_schema=vol.Schema(
                {
                    vol.Required("name"): str,
                    vol.Required("arm_scenario"): selector,
                    vol.Required("disarm_scenario"): selector,
                }
            ),
        )

    async def async_step_panel_add_inverse(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create panels from auto-detected inverse scenario pairs (you pick which).

        Only reachable from the panels menu, which lists this action only when at
        least one inverse pair exists, so ``pairs`` below is never empty.
        """
        pairs = inverse_scenario_pairs(_scenes(self.config_entry))
        labels = {s.id: " ".join(s.label.split()) for s in _scenarios(self.config_entry)}
        if user_input is not None:
            panels = self._current_panels()
            for key in user_input.get("pairs", []):
                arm_id, disarm_id = (int(part) for part in key.split(":"))
                panels.append(
                    PanelDef(
                        id=uuid.uuid4().hex[:8],
                        kind=PanelKind.SCENARIOS,
                        name=labels.get(arm_id) or f"Panel {arm_id}",
                        arm_scenario=arm_id,
                        disarm_scenario=disarm_id,
                    )
                )
            return self._save_panels(panels)
        choices = [
            SelectOptionDict(
                value=f"{arm_id}:{disarm_id}",
                label=f"{labels.get(arm_id) or arm_id} / {labels.get(disarm_id) or disarm_id}",
            )
            for arm_id, disarm_id in pairs
        ]
        return self.async_show_form(
            step_id="panel_add_inverse",
            data_schema=vol.Schema(
                {
                    vol.Required("pairs"): SelectSelector(
                        SelectSelectorConfig(
                            options=choices, multiple=True, mode=SelectSelectorMode.LIST
                        )
                    ),
                }
            ),
        )

    async def async_step_panel_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove one or more existing panels.

        Only reachable from the panels menu, which lists this action only while
        panels exist, so ``panels`` below is never empty.
        """
        panels = self._current_panels()
        if user_input is not None:
            remove = set(user_input.get("remove", []))
            return self._save_panels([p for p in panels if p.id not in remove])
        return self.async_show_form(
            step_id="panel_remove",
            data_schema=vol.Schema(
                {
                    vol.Required("remove"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=p.id, label=p.name) for p in panels
                            ],
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )


def _panel_areas(entry: InimConfigEntry) -> list[Area]:
    """Return the panel areas of a loaded entry, or [] while it is not loaded."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None or runtime.coordinator.data is None:
        return []
    return list(runtime.coordinator.data.areas)


def _scenarios(entry: InimConfigEntry) -> list[Scenario]:
    """Return the panel scenarios (with labels) of a loaded entry, or []."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None or runtime.coordinator.data is None:
        return []
    return list(runtime.coordinator.data.scenarios)


def _scenes(entry: InimConfigEntry) -> list[SceneDef]:
    """Return the scenario definitions (target modes) of a loaded entry, or []."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return []
    return list(runtime.coordinator.local_config.scenes)
