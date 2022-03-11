"""Adds config flow for Nest Protect."""
from __future__ import annotations

from typing import Any, cast

from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .const import CONF_REFRESH_TOKEN, DOMAIN, LOGGER
from .pynest.client import NestClient
from .pynest.exceptions import BadCredentialsException


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Nest Protect."""

    VERSION = 1

    _config_entry: ConfigEntry | None

    def __init__(self) -> None:
        """Initialize Nest Protect Config Flow."""
        super().__init__()

        self._config_entry = None

    async def async_validate_input(self, user_input: dict[str, Any]) -> None:
        """Validate user credentials."""

        session = async_get_clientsession(self.hass)
        client = NestClient(session)
        token = user_input[CONF_TOKEN]

        refresh_token = await client.get_refresh_token(token)
        auth = await client.get_access_token(refresh_token)

        await client.authenticate(
            auth.access_token
        )  # TODO use result to gather more details

        # TODO change unique id to an id related to the nest account
        await self.async_set_unique_id(user_input[CONF_TOKEN])

        return refresh_token

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initialized by the user."""
        errors = {}

        if user_input:
            try:
                refresh_token = await self.async_validate_input(user_input)
                user_input[CONF_REFRESH_TOKEN] = refresh_token
            except (TimeoutError, ClientError):
                errors["base"] = "cannot_connect"
            except BadCredentialsException:
                errors["base"] = "invalid_auth"
            except Exception as exception:  # pylint: disable=broad-except
                errors["base"] = "unknown"
                LOGGER.exception(exception)
            else:
                if self._config_entry:
                    # Update existing entry during reauth
                    self.hass.config_entries.async_update_entry(
                        self._config_entry,
                        data={
                            **self._config_entry.data,
                            **user_input,
                        },
                    )

                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(
                            self._config_entry.entry_id
                        )
                    )

                    return self.async_abort(reason="reauth_successful")

                self._abort_if_unique_id_configured()

                # TODO pull name from account
                return self.async_create_entry(title="Nest Protect", data=user_input)

        return self.async_show_form(
            step_id="user",
            description_placeholders={"url": NestClient.generate_token_url()},
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TOKEN): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reauth."""
        self._config_entry = cast(
            ConfigEntry,
            self.hass.config_entries.async_get_entry(self.context["entry_id"]),
        )

        return await self.async_step_user(user_input)
