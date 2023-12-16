"""Adds config flow for Nest Protect."""
from __future__ import annotations

from typing import Any, cast

from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .const import (
    CONF_ACCOUNT_TYPE,
    CONF_COOKIES,
    CONF_ISSUE_TOKEN,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    LOGGER,
)
from .pynest.client import NestClient
from .pynest.const import NEST_ENVIRONMENTS
from .pynest.exceptions import BadCredentialsException


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Nest Protect."""

    VERSION = 3

    _config_entry: ConfigEntry | None

    def __init__(self) -> None:
        """Initialize Nest Protect Config Flow."""
        super().__init__()

        self._config_entry = None
        self._default_account_type = "production"
        self._cookies_latest = ""

    async def async_validate_input(self, user_input: dict[str, Any]) -> list:
        """Validate user credentials."""

        environment = user_input[CONF_ACCOUNT_TYPE]
        session = async_get_clientsession(self.hass)
        client = NestClient(session=session, environment=NEST_ENVIRONMENTS[environment])

        if CONF_ISSUE_TOKEN in user_input and CONF_COOKIES in user_input:
            issue_token = user_input[CONF_ISSUE_TOKEN]
            cookies = user_input[CONF_COOKIES]
        if CONF_REFRESH_TOKEN in user_input:
            refresh_token = user_input[CONF_REFRESH_TOKEN]

        if issue_token and cookies:
            auth = await client.get_access_token_from_cookies(issue_token, cookies)
        elif refresh_token:
            auth = await client.get_access_token_from_refresh_token(refresh_token)
        else:
            raise Exception(
                "No cookies, issue token and refresh token, please provide issue_token and cookies or refresh_token"
            )

        response = await client.authenticate(auth.access_token)

        # TODO change unique id to an id related to the nest account
        await self.async_set_unique_id(user_input[CONF_ISSUE_TOKEN])

        return [issue_token, cookies, response.user]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initialized by the user."""
        errors = {}

        if user_input:
            self._default_account_type = user_input[CONF_ACCOUNT_TYPE]
            return await self.async_step_account_link()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ACCOUNT_TYPE, default=self._default_account_type
                    ): vol.In(
                        {key: env.name for key, env in NEST_ENVIRONMENTS.items()}
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_account_link(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initialized by the user."""
        errors = {}
        issue_token = ""
        cookies = ""

        if user_input:
            try:
                user_input[CONF_ACCOUNT_TYPE] = self._default_account_type
                [issue_token, cookies, user] = await self.async_validate_input(
                    user_input
                )
                user_input[CONF_ISSUE_TOKEN] = issue_token
                user_input[CONF_COOKIES] = cookies

                self.check_cookies(cookies)

            except (TimeoutError, ClientError):
                errors["base"] = "cannot_connect"
            except BadCredentialsException:
                errors["base"] = "invalid_auth"
            except ExcessCookiesException:
                errors["base"] = "cookies_unknown"
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

                    return self.async_create_entry(
                        title="Nest Protect", data=user_input, description=user
                    )

                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title="Nest Protect", data=user_input, description=user
                )

        return self.async_show_form(
            step_id="account_link",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ISSUE_TOKEN, default=issue_token): str,
                    vol.Required(CONF_COOKIES, default=cookies): str,
                }
            ),
            errors=errors,
            last_step=True,
        )

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reauth."""
        self._config_entry = cast(
            ConfigEntry,
            self.hass.config_entries.async_get_entry(self.context["entry_id"]),
        )

        self._default_account_type = self._config_entry.data[CONF_ACCOUNT_TYPE]

        return await self.async_step_account_link(user_input)

    def check_cookies(self, cookies):
        """Check for other cookies than five expected ones."""
        # If there are other cookies, it may be an indication of cache not being cleared properly.

        if cookies != self._cookies_latest:
            self._cookies_latest = cookies

            cookie_dict = {
                k.strip(): v.strip()
                for (k, v) in (item.split("=", 1) for item in cookies.split(";"))
            }

            for cookie_name in cookie_dict.keys():
                if cookie_name.upper() not in (
                    cn.upper()
                    for cn in [
                        "NID",
                        "__Secure-3PSID",
                        "__Secure-3PAPISID",
                        "__Host-3PLSID",
                        "__Secure-3PSIDCC",
                    ]
                ):
                    raise ExcessCookiesException

        self._cookies_latest = ""


class ExcessCookiesException(Exception):
    """Raised when there are more then the expected cookies."""

    pass
