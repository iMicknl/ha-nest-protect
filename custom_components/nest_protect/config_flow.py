"""Adds config flow for Nest Protect."""

from __future__ import annotations

from typing import Any, cast

from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession
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
from .pynest.enums import Environment
from .pynest.exceptions import BadCredentialsException


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Nest Protect."""

    VERSION = 3

    _config_entry: ConfigEntry | None = None
    _default_account_type: Environment = Environment.PRODUCTION

    @staticmethod
    def _validate_issue_token(issue_token: str) -> bool:
        """Validate issue token format.

        The issue token URL should be from Google OAuth iframerpc endpoint
        with the issueToken action parameter.
        """
        if not issue_token.startswith("https://accounts.google.com/o/oauth2/iframerpc"):
            return False
        if "action=issueToken" not in issue_token:
            return False
        # Verify it looks like a proper URL with query parameters
        if "?" not in issue_token:
            return False
        return True

    @staticmethod
    def _validate_cookies(cookies: str) -> bool:
        """Validate cookies format.

        Cookies should be substantial, contain key-value pairs,
        and include typical Google auth cookie markers.
        """
        if len(cookies) <= 100:
            return False
        # Require at least one key=value pair
        if "=" not in cookies:
            return False
        # Common Google auth cookie names expected in exported cookie headers
        google_auth_markers = ("APISID=", "SAPISID=", "HSID=", "SSID=", "SID=")
        return any(marker in cookies for marker in google_auth_markers)

    async def async_validate_input(self, user_input: dict[str, Any]) -> list:
        """Validate user credentials."""

        environment = user_input[CONF_ACCOUNT_TYPE]
        session = async_create_clientsession(self.hass)
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

        nest = await client.authenticate(auth.access_token)
        data = await client.get_first_data(nest.access_token, nest.userid)

        email = ""
        for bucket in data.updated_buckets:
            key = bucket.object_key
            if key.startswith("user."):
                email = bucket.value["email"]

        # Set unique id to user_id (object.key: user.xxxx)
        await self.async_set_unique_id(nest.user)

        return [issue_token, cookies, email]

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

        if user_input:
            user_input[CONF_ACCOUNT_TYPE] = self._default_account_type
            issue_token = user_input.get(CONF_ISSUE_TOKEN, "").strip()
            cookies = user_input.get(CONF_COOKIES, "").strip()
            # Store stripped values back so downstream validation and API calls
            # use the normalized credentials
            user_input[CONF_ISSUE_TOKEN] = issue_token
            user_input[CONF_COOKIES] = cookies

            # Validate input format before making API calls
            if not self._validate_issue_token(issue_token):
                errors[CONF_ISSUE_TOKEN] = "invalid_issue_token"
            elif not self._validate_cookies(cookies):
                errors[CONF_COOKIES] = "invalid_cookies"

            if not errors:
                try:
                    [issue_token, cookies, email] = await self.async_validate_input(
                        user_input
                    )
                except (TimeoutError, ClientError):
                    errors["base"] = "cannot_connect"
                except BadCredentialsException:
                    errors["base"] = "invalid_auth"
                except Exception as exception:  # pylint: disable=broad-except
                    errors["base"] = "unknown"
                    LOGGER.exception(exception)

            if not errors:
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

                return self.async_create_entry(
                    title=f"Nest Protect ({email})", data=user_input
                )

        return self.async_show_form(
            step_id="account_link",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ISSUE_TOKEN): str,
                    vol.Required(CONF_COOKIES): str,
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
