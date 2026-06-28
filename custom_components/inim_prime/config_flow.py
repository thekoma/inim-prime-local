"""Config flow for the INIM Prime integration."""

from __future__ import annotations

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
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import (
    ApiStatus,
    InimApiError,
    InimConnectionError,
    InimPrimeClient,
)
from .const import (
    CONF_APIKEY,
    CONF_GROUP_BY_ROOM,
    CONF_LABEL_LANGUAGE,
    CONF_LOCAL_ENABLED,
    CONF_LOCAL_PASSWORD,
    CONF_SCAN_INTERVAL_ACTIVE,
    CONF_SCAN_INTERVAL_IDLE,
    CONF_USE_HTTPS,
    CONF_WEBHOOK_ENABLED,
    CONF_WEBHOOK_ID,
    DEFAULT_GROUP_BY_ROOM,
    DEFAULT_LOCAL_ENABLED,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL_ACTIVE,
    DEFAULT_SCAN_INTERVAL_IDLE,
    DEFAULT_USE_HTTPS,
    DOMAIN,
    LABEL_LANGUAGE_AUTO,
)
from .coordinator import InimConfigEntry
from .zone_guess import SUPPORTED_LANGUAGES


class InimPrimeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for INIM Prime."""

    VERSION = 1

    async def _async_validate(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Validate connection details. Return a (possibly empty) error map."""
        errors: dict[str, str] = {}

        client = InimPrimeClient(
            host=user_input[CONF_HOST],
            port=user_input[CONF_PORT],
            apikey=user_input[CONF_APIKEY],
            session=async_get_clientsession(self.hass),
            use_https=user_input[CONF_USE_HTTPS],
        )

        try:
            await client.version()
        except InimApiError as err:
            if err.status == ApiStatus.ERROR_APIKEY:
                errors["base"] = "invalid_auth"
            else:
                errors["base"] = "unknown"
        except InimConnectionError:
            errors["base"] = "cannot_connect"
        except Exception:  # noqa: BLE001
            errors["base"] = "unknown"

        return errors

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]

            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            errors = await self._async_validate(user_input)
            if not errors:
                return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                    vol.Required(CONF_APIKEY): str,
                    vol.Required(CONF_USE_HTTPS, default=DEFAULT_USE_HTTPS): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle reauthentication when the panel rejects the API key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauthentication by re-entering the API key.

        Host/port/HTTPS are taken from the existing entry (not changeable here);
        only the API key is re-entered and validated against the panel.
        """
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            validate_input = {**entry.data, CONF_APIKEY: user_input[CONF_APIKEY]}
            errors = await self._async_validate(validate_input)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_APIKEY: user_input[CONF_APIKEY]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_APIKEY): str}),
            description_placeholders={CONF_HOST: entry.data[CONF_HOST]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure an existing entry (e.g. rotate the API key)."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            new_unique_id = f"{host}:{port}"

            # unique_id is derived from host:port. Allow it to change as part of
            # the reconfigure, but reject collisions with a *different* entry.
            collision = self.hass.config_entries.async_entry_for_domain_unique_id(
                self.handler, new_unique_id
            )
            if collision is not None and collision.entry_id != entry.entry_id:
                return self.async_abort(reason="already_configured")

            errors = await self._async_validate(user_input)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    title=user_input[CONF_NAME],
                    unique_id=new_unique_id,
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
                        CONF_PORT,
                        default=suggested.get(CONF_PORT, DEFAULT_PORT),
                    ): int,
                    vol.Required(CONF_APIKEY): str,
                    vol.Required(
                        CONF_USE_HTTPS,
                        default=suggested.get(CONF_USE_HTTPS, DEFAULT_USE_HTTPS),
                    ): bool,
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

    Exposes the legacy scan interval plus the adaptive idle/active intervals
    and a "push mode" toggle. Enabling push mode generates a per-entry webhook
    id (the cleartext-LAN shared secret) and surfaces the full local webhook
    URL to the user via a persistent notification so they can paste it into
    PrimeStudio (design doc §2.2/§3).
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        options = dict(self.config_entry.options)

        if user_input is not None:
            new_options = {**options, **user_input}

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

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL_IDLE,
                        default=options.get(CONF_SCAN_INTERVAL_IDLE, DEFAULT_SCAN_INTERVAL_IDLE),
                    ): int,
                    vol.Required(
                        CONF_SCAN_INTERVAL_ACTIVE,
                        default=options.get(
                            CONF_SCAN_INTERVAL_ACTIVE, DEFAULT_SCAN_INTERVAL_ACTIVE
                        ),
                    ): int,
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
                        CONF_LOCAL_ENABLED,
                        default=options.get(CONF_LOCAL_ENABLED, DEFAULT_LOCAL_ENABLED),
                    ): bool,
                    vol.Optional(
                        CONF_LOCAL_PASSWORD,
                        default=options.get(CONF_LOCAL_PASSWORD, ""),
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                }
            ),
        )
