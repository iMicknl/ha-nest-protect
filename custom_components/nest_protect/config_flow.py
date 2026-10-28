"""Adds config flow for Nest Protect."""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import voluptuous as vol
from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
)
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
    CREDENTIAL_KEYS,
    DOMAIN,
    LOGGER,
)
from .pynest.client import NestClient
from .pynest.const import EMBEDDED_SETUP_URL, NEST_ENVIRONMENTS
from .pynest.enums import Environment
from .pynest.exceptions import (
    BadCredentialsException,
    NestServiceException,
)

DESCRIPTION_PLACEHOLDERS = {
    "nest_url": "https://home.nest.com",
    "issue_token_prefix": "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken",
    "accounts_url": "https://accounts.google.com/",
    "embedded_setup_url": EMBEDDED_SETUP_URL,
    # Pinned to a specific release rather than "latest", which may resolve to a
    # pre-release that is incompatible with this version of the integration.
    "extension_download_url": "https://github.com/iMicknl/ha-nest-protect/releases/download/v0.4.4/nest-auth-helper.zip",
}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Nest Protect."""

    VERSION = 3

    _default_account_type: Environment = Environment.PRODUCTION
    # Email, oauth_token, android_id and master_token of the last exchange.
    _exchanged_master_token: tuple[str, str, str, str] | None = None
    _master_token_client: NestClient | None = None

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
        if len(cookies) <= 100:
            return False
        # Require at least one key=value pair
        if "=" not in cookies:
            return False
        # Common Google auth cookie names expected in exported cookie headers
        google_auth_markers = ("APISID=", "SAPISID=", "HSID=", "SSID=", "SID=")
        return any(marker in cookies for marker in google_auth_markers)

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

        user_id, email = await self._async_fetch_account(client, auth.access_token)

        if client.refreshed_cookies:
            cookies = client.refreshed_cookies

        await self.async_set_unique_id(user_id)

        return [issue_token, cookies, email]

    @staticmethod
    async def _async_fetch_account(
        client: NestClient, access_token: str
    ) -> tuple[str, str]:
        """Sign in to Nest and return the Nest user ID (user.xxxx) and email."""
        nest = await client.authenticate(access_token)
        data = await client.get_first_data(nest.access_token, nest.userid)

        email = ""
        for bucket in data.updated_buckets:
            if bucket.object_key.startswith("user."):
                email = bucket.value.get("email", "")

        return nest.user, email

    def _entry_being_updated(self) -> ConfigEntry | None:
        """Return the entry being reauthenticated or reconfigured, if any."""
        if self.source == SOURCE_REAUTH:
            return self._get_reauth_entry()
        if self.source == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        return None

    def _async_create_or_update_entry(
        self, data: dict[str, Any], email: str
    ) -> FlowResult:
        """Create a new entry, or update the entry being reauthenticated/reconfigured."""
        entry = self._entry_being_updated()
        if entry is None:
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=f"Nest Protect ({email})", data=data)

        # Setup prefers some credentials over others, so any left over from a
        # previous method would shadow the ones just entered.
        data = {
            **{k: v for k, v in entry.data.items() if k not in CREDENTIAL_KEYS},
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
                    vol.Required("method", default="extension"): vol.In(
                        {
                            "extension": "Use the Chrome Extension (recommended)",
                            "manual": "Enter credentials manually",
                            "master_token": "Master token (experimental)",
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
        exchanges it for a master token, which lasts until it is revoked, and
        stores that. No browser extension or companion service is
        used; renewal happens entirely inside HA.
        """
        errors: dict[str, str] = {}
        environment = self._default_account_type

        if user_input:
            email = user_input[CONF_GOOGLE_EMAIL].strip()
            oauth_token = user_input[CONF_OAUTH_TOKEN].strip()
            # Keep presenting the same device to Google across reauths, as
            # ha-google-home does.
            entry = self._entry_being_updated()
            android_id = (
                entry and entry.data.get(CONF_ANDROID_ID)
            ) or NestClient.generate_android_id()
            user_id = None

            if not email or not oauth_token:
                errors["base"] = "invalid_auth"

            if not errors:
                # Kept across retries, so a gpsoauth call still hanging from the
                # last attempt blocks a new one instead of abandoning another thread.
                if self._master_token_client is None:
                    self._master_token_client = NestClient(
                        session=async_create_clientsession(self.hass),
                        environment=NEST_ENVIRONMENTS[environment],
                    )
                client = self._master_token_client
                try:
                    # Google accepts an oauth_token only once, so a retry after a
                    # later step failed reuses the master token it was exchanged for.
                    exchanged = self._exchanged_master_token
                    if exchanged and exchanged[:2] == (email, oauth_token):
                        *_, android_id, master_token = exchanged
                    else:
                        master_token = await client.exchange_master_token(
                            oauth_token, email, android_id
                        )
                        self._exchanged_master_token = (
                            email,
                            oauth_token,
                            android_id,
                            master_token,
                        )
                    auth = await client.get_access_token_from_master_token(
                        master_token, email, android_id
                    )
                    user_id, account_email = await self._async_fetch_account(
                        client, auth.access_token
                    )
                except TimeoutError, ClientError, NestServiceException:
                    errors["base"] = "cannot_connect"
                except BadCredentialsException:
                    errors["base"] = "invalid_auth"
                except Exception as exception:  # pylint: disable=broad-except
                    errors["base"] = "unknown"
                    LOGGER.exception(exception)

            if not errors and user_id is not None:
                await self.async_set_unique_id(user_id)

                new_data = {
                    CONF_MASTER_TOKEN: master_token,
                    CONF_GOOGLE_EMAIL: email,
                    CONF_ANDROID_ID: android_id,
                    CONF_ACCOUNT_TYPE: environment,
                }

                return self._async_create_or_update_entry(
                    new_data, account_email or email
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
