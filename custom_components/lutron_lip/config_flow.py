"""Config flow to configure the Lutron integration."""

from __future__ import annotations
import contextlib
import logging
from typing import Any
from urllib.error import HTTPError

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from . import LutronController, LutronData
from .aiolip.exceptions import LIPLoginError
from .const import (
    CONF_DEFAULT_DIMMER_LEVEL,
    CONF_REFRESH_DATA,
    CONF_SUGGEST_AREAS,
    CONF_USE_AREA_FOR_DEVICE_NAME,
    CONF_USE_FULL_PATH,
    CONF_USE_RADIORA_MODE,
    CONF_VARIABLE_IDS,
    DEFAULT_DIMMER_LEVEL,
    DOMAIN,
    LUTRON_DATA_FILE,
)

_LOGGER = logging.getLogger(__name__)


def get_lutron_covers(hass, config_entry):
    """Return lutron cover entities."""
    # select only is_motor and fix entity_id
    entry_data: LutronData = hass.data[DOMAIN][config_entry.entry_id]
    return [
        {"legacy_uuid": f"cover.{device.legacy_uuid}", "name": device.name}
        for device in entry_data.covers
    ]


async def async_try_login(
    hass: HomeAssistant, host: str, username: str, password: str
) -> str | None:
    """
    Log in over LIP once and log out: return None, or the form error to show.

    The system data (the XML) downloads without any login, so before this the
    form accepted a wrong username or password and the entry failed later with
    a protocol error (2026-10-13, 9360-balada).
    """
    controller = LutronController(
        hass,
        host,
        username,
        password,
        use_full_path=False,
        use_area_for_device_name=False,
        use_radiora_mode=False,
    )
    try:
        await controller.connect()
    except LIPLoginError:
        return "invalid_auth"
    except Exception:  # noqa: BLE001 -- unreachable, refused, timed out, not LIP
        _LOGGER.debug("Lutron LIP login test to %s failed", host, exc_info=True)
        return "cannot_connect"
    finally:
        if controller.lip is not None:
            with contextlib.suppress(Exception):
                await controller.lip.async_stop()
    return None


class LutronRonModifiedConfigFlow(ConfigFlow, domain=DOMAIN):
    """User prompt for Main Repeater configuration information."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First step in the config flow."""
        errors = {}

        if user_input is not None:
            ip_address = user_input[CONF_HOST]
            username = user_input.get(CONF_USERNAME)
            password = user_input.get(CONF_PASSWORD)
            use_full_path = user_input.get(CONF_USE_FULL_PATH)
            use_area_for_device_name = user_input.get(CONF_USE_AREA_FOR_DEVICE_NAME)
            use_radiora_mode = user_input.get(CONF_USE_RADIORA_MODE)

            lutron_controller = LutronController(
                self.hass,
                ip_address,
                username,
                password,
                use_full_path,
                use_area_for_device_name,
                use_radiora_mode,
            )

            try:
                lutron_data_file = self.hass.config.path(LUTRON_DATA_FILE)
                refresh_data = user_input.get(CONF_REFRESH_DATA)
                variable_ids = [
                    int(v.strip())
                    for v in user_input.get(CONF_VARIABLE_IDS, "").split(",")
                    if v.strip().isdigit()
                ]

                await self.hass.async_add_executor_job(
                    lambda: lutron_controller.load_xml_db(
                        lutron_data_file,
                        refresh_data,
                        variable_ids=variable_ids,
                    )
                )
            except HTTPError:
                _LOGGER.exception("Http error")
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unknown error")
                errors["base"] = "unknown"
            else:
                guid = lutron_controller.guid
                if len(guid) <= 10:
                    errors["base"] = "cannot_connect"

            if not errors and (
                login_error := await async_try_login(
                    self.hass, ip_address, username, password
                )
            ):
                errors["base"] = login_error

            if not errors:
                await self.async_set_unique_id(guid)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(title="Lutron", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_USERNAME, default="lutron"): str,
                    vol.Required(CONF_PASSWORD, default="integration"): str,
                    vol.Required(CONF_REFRESH_DATA, default=True): bool,
                    vol.Required(CONF_USE_FULL_PATH, default=False): bool,
                    vol.Required(CONF_USE_AREA_FOR_DEVICE_NAME, default=False): bool,
                    vol.Required(CONF_USE_RADIORA_MODE, default=False): bool,
                    vol.Optional(CONF_VARIABLE_IDS, default=""): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, _entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start re-authentication: the processor refused the stored login."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the integration login again and test it before saving."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if login_error := await async_try_login(
                self.hass,
                entry.data[CONF_HOST],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            ):
                errors["base"] = login_error
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={"host": entry.data[CONF_HOST]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlowHandler:
        """Get the options flow for this handler."""
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlow):
    """Handle option flow for Lutron."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options flow."""
        if user_input is not None:
            return self.async_create_entry(title="Lutron", data=user_input)

        config = {**self.config_entry.data, **self.config_entry.options}

        covers = get_lutron_covers(self.hass, self.config_entry)

        schema_dict = {
            vol.Required(
                CONF_REFRESH_DATA, default=config.get(CONF_REFRESH_DATA, True)
            ): bool,
            vol.Required(
                CONF_USE_FULL_PATH, default=config.get(CONF_USE_FULL_PATH, False)
            ): bool,
            vol.Required(
                CONF_USE_AREA_FOR_DEVICE_NAME,
                default=config.get(CONF_USE_AREA_FOR_DEVICE_NAME, False),
            ): bool,
            vol.Required(
                CONF_USE_RADIORA_MODE, default=config.get(CONF_USE_RADIORA_MODE, False)
            ): bool,
            vol.Required(
                CONF_SUGGEST_AREAS, default=config.get(CONF_SUGGEST_AREAS, True)
            ): bool,
            vol.Optional(
                CONF_VARIABLE_IDS, default=config.get(CONF_VARIABLE_IDS, "")
            ): str,
            vol.Required(
                CONF_DEFAULT_DIMMER_LEVEL,
                default=config.get(CONF_DEFAULT_DIMMER_LEVEL, DEFAULT_DIMMER_LEVEL),
            ): NumberSelector(
                NumberSelectorConfig(min=1, max=255, mode=NumberSelectorMode.SLIDER)
            ),
        }

        # Append per-cover travel time options
        for cover in covers:
            legacy_uuid = cover["legacy_uuid"]
            name = cover["name"]
            key = f"{name}_travel_time"
            default = config.get(key, 10)  # 10 seconds default travel time
            schema_dict[
                vol.Required(
                    key,
                    default=default,
                    description={
                        "suggested_value": default,
                        "identifier": key,
                        "name": f"Travel time for {name}",
                    },
                )
            ] = NumberSelector(
                NumberSelectorConfig(min=1, max=120, mode=NumberSelectorMode.BOX)
            )

        data_schema = vol.Schema(schema_dict)

        return self.async_show_form(step_id="init", data_schema=data_schema)
