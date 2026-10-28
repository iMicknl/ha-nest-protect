"""Adds config flow for Nest Protect."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Collection
from typing import Any

import voluptuous as vol
from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import (
    CONF_ACCOUNT_TYPE,
    CONF_ANDROID_ID,
    CONF_AUTH_CODE,
    CONF_COOKIES,
    CONF_GOOGLE_EMAIL,
    CONF_ISSUE_TOKEN,
    CONF_MASTER_TOKEN,
    CONF_OAUTH_TOKEN,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    LOGGER,
)
from .pynest.client import NestClient
from .pynest.const import NEST_ENVIRONMENTS
from .pynest.enums import Environment
from .pynest.exceptions import (
    BadCredentialsException,
    NestServiceException,
    PynestException,
)

DESCRIPTION_PLACEHOLDERS = {
    "nest_url": "https://home.nest.com",
    "issue_token_prefix": "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken",
    "accounts_url": "https://accounts.google.com/",
    "embedded_setup_url": "https://accounts.google.com/EmbeddedSetup",
    # Pinned to a specific release rather than "latest", which may resolve to a
    # pre-release that is incompatible with this version of the integration.
    "extension_download_url": "https://github.com/iMicknl/ha-nest-protect/releases/download/v0.4.4/nest-auth-helper.zip",
}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Nest Protect."""

    VERSION = 3

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
        return "?" in issue_token

    @staticmethod
    def _validate_cookies(cookies: str) -> bool:
        """Validate cookies format.

        Cookies should be substantial, contain key-value pairs,
        and include typical Google auth cookie markers.
        """
        if len(cookies) < 500:
            return False
        if "=" not in cookies:
            return False
        google_auth_markers = ("APISID=", "SAPISID=", "HSID=", "SSID=", "SID=")
        if not any(marker in cookies for marker in google_auth_markers):
            return False
        extended_markers = ("SIDCC=", "__Secure-")
        return any(marker in cookies for marker in extended_markers)

    @staticmethod
    def _normalize_cookies(cookies: str) -> str:
        """Normalize a pasted cookie header.

        Long header values copied from browser developer tools can contain line
        wraps, and some browsers include the header name in the copied text.
        """
        cookies = "".join(line.strip() for line in cookies.splitlines())
        cookies = re.sub(r"^cookie:\s*", "", cookies, flags=re.IGNORECASE)
        return "; ".join(part.strip() for part in cookies.split(";") if part.strip())

    async def async_validate_input(self, user_input: dict[str, Any]) -> list:
        """Validate user credentials."""

        environment = user_input[CONF_ACCOUNT_TYPE]
        session = async_create_clientsession(self.hass)
        client = NestClient(session=session, environment=NEST_ENVIRONMENTS[environment])

        issue_token = user_input.get(CONF_ISSUE_TOKEN)
        cookies = user_input.get(CONF_COOKIES)
        refresh_token = user_input.get(CONF_REFRESH_TOKEN)

        if issue_token and cookies:
            auth = await client.get_access_token_from_cookies(issue_token, cookies)
        elif refresh_token:
            auth = await client.get_access_token_from_refresh_token(refresh_token)

        nest = await client.authenticate(auth.access_token)
        data = await client.get_first_data(nest.access_token, nest.userid)

        if client.refreshed_cookies:
            cookies = client.refreshed_cookies

        email = ""
        for bucket in data.updated_buckets:
            key = bucket.object_key
            if key.startswith("user."):
                email = bucket.value["email"]

        # Set unique id to user_id (object.key: user.xxxx)
        await self.async_set_unique_id(nest.user)

        return [issue_token, cookies, email]

    def _async_create_or_update_entry(
        self,
        data: dict[str, Any],
        email: str,
        drop_keys: Collection[str] = (),
    ) -> FlowResult:
        """Create a new entry, or update the entry being reauthenticated/reconfigured."""
        if self.source not in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=f"Nest Protect ({email})", data=data)

        entry = (
            self._get_reauth_entry()
            if self.source == SOURCE_REAUTH
            else self._get_reconfigure_entry()
        )
        data = {
            **{k: v for k, v in entry.data.items() if k not in drop_keys},
            **data,
        }

        # Entries created before v0.4.0 are keyed by their refresh token instead
        # of the Nest user ID, so the account can't be compared. Adopt the account
        # that was just authenticated, unless another entry already owns it.
        if not entry.unique_id or not entry.unique_id.startswith("user."):
            if self.hass.config_entries.async_entry_for_domain_unique_id(
                DOMAIN, self.unique_id
            ):
                return self.async_abort(reason="already_configured")
            return self.async_update_reload_and_abort(
                entry, unique_id=self.unique_id, data=data
            )

        self._abort_if_unique_id_mismatch(reason="wrong_account")
        return self.async_update_reload_and_abort(entry, data=data)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initialized by the user."""
        errors = {}

        if user_input:
            self._default_account_type = user_input[CONF_ACCOUNT_TYPE]
            return await self.async_step_auth_method()

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

    async def async_step_auth_method(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle auth method selection."""
        if user_input:
            method = user_input["method"]
            if method == "master_token":
                return await self.async_step_master_token()
            if method == "extension":
                return await self.async_step_extension()
            return await self.async_step_account_link()

        return self.async_show_form(
            step_id="auth_method",
            data_schema=vol.Schema(
                {
                    vol.Required("method", default="master_token"): vol.In(
                        {
                            "master_token": "Master token (stays logged in, recommended)",
                            "extension": "Use the Chrome Extension",
                            "manual": "Enter credentials manually",
                        }
                    ),
                }
            ),
            description_placeholders=DESCRIPTION_PLACEHOLDERS,
        )

    async def async_step_master_token(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle authentication via the durable Google master-token flow.

        The user provides their Google email and a one-time ``oauth_token``
        captured from ``accounts.google.com/EmbeddedSetup``. Home Assistant
        exchanges it for a master token (which never expires unless the password
        changes) and stores that. No browser extension or companion service is
        used; renewal happens entirely inside HA.
        """
        errors: dict[str, str] = {}
        environment = self._default_account_type

        if user_input:
            email = user_input[CONF_GOOGLE_EMAIL].strip()
            oauth_token = user_input[CONF_OAUTH_TOKEN].strip()
            android_id = NestClient.generate_android_id()
            nest = None
            data = None

            if not email or not oauth_token:
                errors["base"] = "invalid_code"

            if not errors:
                session = async_create_clientsession(self.hass)
                client = NestClient(
                    session=session, environment=NEST_ENVIRONMENTS[environment]
                )
                try:
                    await client.exchange_master_token(oauth_token, email, android_id)
                    auth = await client.get_access_token_from_master_token()
                    nest = await client.authenticate(auth.access_token)
                    data = await client.get_first_data(nest.access_token, nest.userid)
                except TimeoutError, ClientError, NestServiceException:
                    errors["base"] = "cannot_connect"
                except BadCredentialsException:
                    errors["base"] = "invalid_auth"
                except PynestException:
                    errors["base"] = "unknown"
                except Exception as exception:  # pylint: disable=broad-except
                    errors["base"] = "unknown"
                    LOGGER.exception(exception)

            if not errors and nest is not None and data is not None:
                account_email = email
                for bucket in data.updated_buckets:
                    if bucket.object_key.startswith("user."):
                        account_email = bucket.value.get("email", email)

                await self.async_set_unique_id(nest.user)

                new_data = {
                    CONF_MASTER_TOKEN: client.master_token,
                    CONF_GOOGLE_EMAIL: email,
                    CONF_ANDROID_ID: android_id,
                    CONF_ACCOUNT_TYPE: environment,
                }

                # Switch to the durable credential and drop the legacy ones.
                return self._async_create_or_update_entry(
                    new_data,
                    account_email,
                    drop_keys=(CONF_ISSUE_TOKEN, CONF_COOKIES, CONF_REFRESH_TOKEN),
                )

        return self.async_show_form(
            step_id="master_token",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_GOOGLE_EMAIL): str,
                    vol.Required(CONF_OAUTH_TOKEN): str,
                }
            ),
            description_placeholders=DESCRIPTION_PLACEHOLDERS,
            errors=errors,
            last_step=True,
        )

    async def async_step_extension(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle authentication via Chrome extension code."""
        errors = {}

        if user_input:
            issue_token = ""
            cookies = ""

            try:
                decoded = json.loads(
                    base64.b64decode(user_input[CONF_AUTH_CODE]).decode()
                )
                issue_token = decoded["issue_token"]
                cookies = decoded["cookies"]
            except ValueError, KeyError, json.JSONDecodeError:
                errors[CONF_AUTH_CODE] = "invalid_code"

            if not errors and (
                not self._validate_issue_token(issue_token)
                or not self._validate_cookies(cookies)
            ):
                errors[CONF_AUTH_CODE] = "invalid_code"

            if not errors:
                validation_input = {
                    CONF_ISSUE_TOKEN: issue_token,
                    CONF_COOKIES: cookies,
                    CONF_ACCOUNT_TYPE: self._default_account_type,
                }
                try:
                    [issue_token, cookies, email] = await self.async_validate_input(
                        validation_input
                    )
                except TimeoutError, ClientError, NestServiceException:
                    errors["base"] = "cannot_connect"
                except BadCredentialsException:
                    errors["base"] = "invalid_auth"
                except Exception as exception:  # pylint: disable=broad-except
                    errors["base"] = "unknown"
                    LOGGER.exception(exception)

            if not errors:
                data = {
                    CONF_ISSUE_TOKEN: issue_token,
                    CONF_COOKIES: cookies,
                    CONF_ACCOUNT_TYPE: self._default_account_type,
                }
                return self._async_create_or_update_entry(data, email)

        return self.async_show_form(
            step_id="extension",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AUTH_CODE): str,
                }
            ),
            description_placeholders=DESCRIPTION_PLACEHOLDERS,
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
            cookies = self._normalize_cookies(user_input.get(CONF_COOKIES, ""))
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
                    user_input[CONF_ISSUE_TOKEN] = issue_token
                    user_input[CONF_COOKIES] = cookies
                except TimeoutError, ClientError, NestServiceException:
                    errors["base"] = "cannot_connect"
                except BadCredentialsException:
                    errors["base"] = "invalid_auth"
                except Exception as exception:  # pylint: disable=broad-except
                    errors["base"] = "unknown"
                    LOGGER.exception(exception)

            if not errors:
                return self._async_create_or_update_entry(user_input, email)

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
        self._default_account_type = self._get_reauth_entry().data[CONF_ACCOUNT_TYPE]

        return await self.async_step_auth_method()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle updating the credentials of an existing entry."""
        self._default_account_type = self._get_reconfigure_entry().data[
            CONF_ACCOUNT_TYPE
        ]

        return await self.async_step_auth_method()
