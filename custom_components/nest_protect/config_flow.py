"""Adds config flow for Nest Protect."""
from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_TOKEN
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from custom_components.nest_protect.pynest.client import NestClient

from .const import CONF_REFRESH_TOKEN, DOMAIN, LOGGER


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Nest Protect."""

    VERSION = 1

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

        return refresh_token

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""
        errors = {}

        if user_input:
            try:
                refresh_token = await self.async_validate_input(user_input)
                user_input[CONF_REFRESH_TOKEN] = refresh_token
                # TODO catch more specific exceptions when pynest supports this
            except Exception as exception:  # pylint: disable=broad-except
                errors["base"] = "unknown"
                LOGGER.exception(exception)
            else:
                # TODO change unique id to an id related to the nest account
                await self.async_set_unique_id(user_input[CONF_TOKEN])
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
