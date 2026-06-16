"""Adds config flow for Nest Protect."""

from __future__ import annotations

import base64
import json
from typing import Any, cast

import voluptuous as vol
from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import (
    CONF_ACCOUNT_TYPE,
    CONF_AUTH_CODE,
    CONF_COOKIES,
    CONF_ISSUE_TOKEN,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    ISSUE_COOKIE_EXPIRED,
    LOGGER,
)
from .pynest.client import NestClient
from .pynest.const import NEST_ENVIRONMENTS
from .pynest.enums import Environment
from .pynest.exceptions import BadCredentialsException, PynestException

DESCRIPTION_PLACEHOLDERS = {
    "nest_url": "https://home.nest.com",
    "issue_token_prefix": "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken",
    "accounts_url": "https://accounts.google.com/",
    # Pinned to a specific release rather than "latest", which may resolve to a
    # pre-release that is incompatible with this version of the integration.
    "extension_download_url": "https://github.com/iMicknl/ha-nest-protect/releases/download/v0.4.4/nest-auth-helper.zip",
}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Nest Protect."""

    VERSION = 3

    _config_entry: ConfigEntry | None = None
    _default_account_type: Environment = Environment.PRODUCTION

    # State for the installed-app PKCE (app token) flow.
    _app_token_code_verifier: str | None = None
    _app_token_redirect_uri: str | None = None
    _app_token_auth_url: str | None = None

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

    def _clear_cookie_expired_issue(self, entry: ConfigEntry) -> None:
        """Remove cookie-expired repair issue after successful reauth."""
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_COOKIE_EXPIRED}_{entry.entry_id}"
        )

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
            if method == "app_token":
                return await self.async_step_app_token()
            if method == "extension":
                return await self.async_step_extension()
            return await self.async_step_account_link()

        return self.async_show_form(
            step_id="auth_method",
            data_schema=vol.Schema(
                {
                    vol.Required("method", default="app_token"): vol.In(
                        {
                            "app_token": "App token (stays logged in, recommended)",
                            "extension": "Use the Chrome Extension",
                            "manual": "Enter credentials manually",
                        }
                    ),
                }
            ),
            description_placeholders=DESCRIPTION_PLACEHOLDERS,
        )

    async def async_step_app_token(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle authentication via the installed-app (PKCE) token flow.

        This mints a long-lived refresh token entirely inside Home Assistant,
        with no browser extension and no companion service. The credential only
        becomes invalid on a Google password change or explicit revocation.
        """
        errors: dict[str, str] = {}
        environment = self._default_account_type
        client_id = NEST_ENVIRONMENTS[environment].client_id

        # Generate a fresh PKCE pair and authorization URL on first display.
        if self._app_token_code_verifier is None:
            code_verifier, code_challenge = NestClient.generate_pkce_pair()
            self._app_token_code_verifier = code_verifier
            self._app_token_redirect_uri = NestClient.build_redirect_uri(client_id)
            self._app_token_auth_url = NestClient.build_authorization_url(
                client_id, code_challenge, self._app_token_redirect_uri
            )

        if user_input:
            code = NestClient.extract_authorization_code(user_input[CONF_AUTH_CODE])
            nest = None
            data = None

            if not code:
                errors[CONF_AUTH_CODE] = "invalid_code"

            if not errors:
                session = async_create_clientsession(self.hass)
                client = NestClient(
                    session=session, environment=NEST_ENVIRONMENTS[environment]
                )
                try:
                    auth = await client.exchange_authorization_code(
                        code,
                        cast(str, self._app_token_code_verifier),
                        cast(str, self._app_token_redirect_uri),
                    )
                    nest = await client.authenticate(auth.access_token)
                    data = await client.get_first_data(nest.access_token, nest.userid)
                except (TimeoutError, ClientError):
                    errors["base"] = "cannot_connect"
                except BadCredentialsException:
                    errors[CONF_AUTH_CODE] = "invalid_code"
                except PynestException:
                    errors[CONF_AUTH_CODE] = "invalid_code"
                except Exception as exception:  # pylint: disable=broad-except
                    errors["base"] = "unknown"
                    LOGGER.exception(exception)

            if not errors and nest is not None and data is not None:
                email = ""
                for bucket in data.updated_buckets:
                    if bucket.object_key.startswith("user."):
                        email = bucket.value["email"]

                await self.async_set_unique_id(nest.user)

                new_data = {
                    CONF_REFRESH_TOKEN: client.refresh_token,
                    CONF_ACCOUNT_TYPE: environment,
                }

                # Reset PKCE state so a retry/new flow generates a fresh code.
                self._app_token_code_verifier = None

                if self._config_entry:
                    # Reauth: replace credentials and drop any stale cookie creds.
                    merged = {
                        key: value
                        for key, value in self._config_entry.data.items()
                        if key not in (CONF_ISSUE_TOKEN, CONF_COOKIES)
                    }
                    merged.update(new_data)
                    self.hass.config_entries.async_update_entry(
                        self._config_entry, data=merged
                    )
                    self._clear_cookie_expired_issue(self._config_entry)
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(
                            self._config_entry.entry_id
                        )
                    )
                    return self.async_abort(reason="reauth_successful")

                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Nest Protect ({email})", data=new_data
                )

        return self.async_show_form(
            step_id="app_token",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AUTH_CODE): str,
                }
            ),
            description_placeholders={
                **DESCRIPTION_PLACEHOLDERS,
                "auth_url": self._app_token_auth_url or "",
            },
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
            except (ValueError, KeyError, json.JSONDecodeError):
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
                except (TimeoutError, ClientError):
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

                if self._config_entry:
                    self.hass.config_entries.async_update_entry(
                        self._config_entry,
                        data={**self._config_entry.data, **data},
                    )
                    self._clear_cookie_expired_issue(self._config_entry)
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(
                            self._config_entry.entry_id
                        )
                    )
                    return self.async_abort(reason="reauth_successful")

                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Nest Protect ({email})", data=data
                )

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
                    self._clear_cookie_expired_issue(self._config_entry)

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

        return await self.async_step_auth_method()

    async def async_step_fix(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle repair flow when Google cookies have expired."""
        entry_id = self.context.get("entry_id") or self.context.get(
            "issue_data", {}
        ).get("entry_id")
        if not entry_id:
            return self.async_abort(reason="unknown")

        self._config_entry = cast(
            ConfigEntry,
            self.hass.config_entries.async_get_entry(entry_id),
        )
        if self._config_entry is None:
            return self.async_abort(reason="unknown")

        self._default_account_type = self._config_entry.data[CONF_ACCOUNT_TYPE]
        return await self.async_step_auth_method()
