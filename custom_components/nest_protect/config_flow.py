"""Adds config flow for Nest Protect."""

from __future__ import annotations

from typing import Any, cast

import voluptuous as vol
from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .auth_view import async_get_auth_view
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

DESCRIPTION_PLACEHOLDERS = {
    "nest_url": "https://home.nest.com",
    "issue_token_prefix": "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken",
    "accounts_url": "https://accounts.google.com/",
}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Nest Protect."""

    VERSION = 3

    _config_entry: ConfigEntry | None = None
    _default_account_type: Environment = Environment.PRODUCTION
    _extension_credentials: dict[str, Any] | None = None

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
        return "?" in issue_token

    @staticmethod
    def _validate_cookies(cookies: str) -> bool:
        """Validate cookies format.

        Cookies should be substantial, contain key-value pairs,
        and include typical Google auth cookie markers.
        """
        if len(cookies) <= 100:
            return False
        if "=" not in cookies:
            return False
        google_auth_markers = (
            "APISID=",
            "SAPISID=",
            "HSID=",
            "SSID=",
            "SID=",
            "__Secure-1PSID=",
            "__Secure-3PSID=",
        )
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
            method = user_input.get("setup_method", "extension")
            if method == "extension":
                return await self.async_step_extension()
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
                    vol.Required("setup_method", default="extension"): vol.In(
                        {
                            "extension": "Chrome Extension (recommended)",
                            "manual": "Manual (issue_token + cookies)",
                        }
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_extension(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show extension instructions and wait for callback."""
        view = async_get_auth_view(self.hass)
        view.register_flow(self.flow_id)

        return self.async_external_step(
            step_id="extension_wait", url="https://home.nest.com"
        )

    async def async_step_extension_wait(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle credentials received from the extension."""
        view = async_get_auth_view(self.hass)
        view.unregister_flow(self.flow_id)

        if not user_input:
            return self.async_abort(reason="extension_timeout")

        self._extension_credentials = user_input
        return self.async_external_step_done(next_step_id="extension_finish")

    async def async_step_extension_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Validate credentials received from the extension."""
        issue_token = self._extension_credentials[CONF_ISSUE_TOKEN]
        cookies = self._extension_credentials[CONF_COOKIES]

        session = async_create_clientsession(self.hass)
        client = NestClient(
            session=session,
            environment=NEST_ENVIRONMENTS[self._default_account_type],
        )

        try:
            auth = await client.get_access_token_from_cookies(issue_token, cookies)
            nest = await client.authenticate(auth.access_token)
        except BadCredentialsException as exc:
            LOGGER.error("Extension auth failed (invalid credentials): %s", exc)
            return self.async_abort(reason="invalid_auth")
        except TimeoutError, ClientError:
            return self.async_abort(reason="cannot_connect")
        except Exception as exception:
            LOGGER.exception(exception)
            return self.async_abort(reason="unknown")

        data = await client.get_first_data(nest.access_token, nest.userid)
        email = ""
        for bucket in data.updated_buckets:
            if bucket.object_key.startswith("user."):
                email = bucket.value["email"]
                break

        await self.async_set_unique_id(nest.user)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=f"Nest Protect ({email})",
            data={
                CONF_ACCOUNT_TYPE: self._default_account_type,
                CONF_ISSUE_TOKEN: issue_token,
                CONF_COOKIES: cookies,
            },
        )

    async def async_step_account_link(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle manual credential entry."""
        errors = {}

        if user_input:
            user_input[CONF_ACCOUNT_TYPE] = self._default_account_type
            issue_token = user_input.get(CONF_ISSUE_TOKEN, "").strip()
            cookies = user_input.get(CONF_COOKIES, "").strip()
            user_input[CONF_ISSUE_TOKEN] = issue_token
            user_input[CONF_COOKIES] = cookies

            if not self._validate_issue_token(issue_token):
                errors[CONF_ISSUE_TOKEN] = "invalid_issue_token"
            elif not self._validate_cookies(cookies):
                errors[CONF_COOKIES] = "invalid_cookies"

            if not errors:
                try:
                    [issue_token, cookies, email] = await self.async_validate_input(
                        user_input
                    )
                except TimeoutError, ClientError:
                    errors["base"] = "cannot_connect"
                except BadCredentialsException:
                    errors["base"] = "invalid_auth"
                except Exception as exception:  # pylint: disable=broad-except
                    errors["base"] = "unknown"
                    LOGGER.exception(exception)

            if not errors:
                if self._config_entry:
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
            description_placeholders=DESCRIPTION_PLACEHOLDERS,
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
