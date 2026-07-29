"""Adds config flow for Nest Protect."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

import voluptuous as vol
from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.storage import Store

from .const import (
    CONF_ACCOUNT_TYPE,
    CONF_AUTH_CODE,
    CONF_AUTH_GENERATION,
    CONF_COOKIES,
    CONF_ISSUE_TOKEN,
    DOMAIN,
    LOGGER,
    STORAGE_KEY_FORMAT,
    STORAGE_PENDING_REAUTH_KEY_FORMAT,
    STORAGE_VERSION,
)
from .credentials import (
    apply_credential_updates,
    create_credential_providers,
    credential_field_names,
)
from .pynest.client import NestClient
from .pynest.const import NEST_ENVIRONMENTS
from .pynest.enums import Environment
from .pynest.exceptions import BadCredentialsException
from .session import (
    async_authenticate_with_providers_once,
    build_reauthentication_state,
)

DESCRIPTION_PLACEHOLDERS = {
    "nest_url": "https://home.nest.com",
    "issue_token_prefix": "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken",
    "accounts_url": "https://accounts.google.com/",
    # Pinned to a specific release rather than "latest", which may resolve to a
    # pre-release that is incompatible with this version of the integration.
    "extension_download_url": "https://github.com/iMicknl/ha-nest-protect/releases/download/v0.4.4/nest-auth-helper.zip",
}


@dataclass(frozen=True)
class ConfigFlowValidationResult:
    """Validated provider credentials and the Nest account they access."""

    credentials: dict[str, Any]
    credential_fields: frozenset[str]
    email: str


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Nest Protect."""

    VERSION = 3

    _config_entry: ConfigEntry | None = None
    _default_account_type: Environment = Environment.PRODUCTION
    _pending_credential_source_fingerprint: str | None = None
    _pending_credential_updates: dict[str, Any] | None = None

    def _pending_reauthentication_store(self) -> Store | None:
        """Return durable scratch storage while reauthentication is validated."""
        if self._config_entry is None:
            return None
        return Store(
            self.hass,
            STORAGE_VERSION,
            STORAGE_PENDING_REAUTH_KEY_FORMAT.format(
                entry_id=self._config_entry.entry_id
            ),
        )

    @staticmethod
    def _credential_source_fingerprint(
        environment: Any,
        credentials: Mapping[str, Any],
        auth_generation: Any,
    ) -> str:
        """Identify the submitted credential set without storing another copy."""
        serialized = json.dumps(
            {
                "environment": environment,
                "credentials": credentials,
                "auth_generation": auth_generation,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(serialized.encode()).hexdigest()

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

    async def async_validate_input(
        self, user_input: dict[str, Any]
    ) -> ConfigFlowValidationResult:
        """Validate user credentials."""

        environment = user_input[CONF_ACCOUNT_TYPE]
        session = async_create_clientsession(self.hass)
        client = NestClient(session=session, environment=NEST_ENVIRONMENTS[environment])

        pending_store = self._pending_reauthentication_store()
        entry_generation = (
            self._config_entry.data.get(CONF_AUTH_GENERATION)
            if self._config_entry is not None
            else None
        )
        pending_updates: dict[str, Any] = {}
        credential_source_fingerprint = ""

        async def capture_credential_updates(updates) -> None:
            pending_updates.update(updates)
            if pending_store is not None:
                await pending_store.async_save(
                    {
                        "source": credential_source_fingerprint,
                        "generation": entry_generation,
                        "updates": dict(pending_updates),
                    }
                )

        providers = create_credential_providers(
            client,
            capture_credential_updates,
        )
        fields = credential_field_names(providers)
        submitted_credentials = {
            field: user_input[field] for field in fields if field in user_input
        }
        credential_inputs = {
            field: submitted_credentials.get(field) for field in fields
        }
        credential_source_fingerprint = self._credential_source_fingerprint(
            environment,
            credential_inputs,
            entry_generation,
        )
        if credential_source_fingerprint != self._pending_credential_source_fingerprint:
            self._pending_credential_source_fingerprint = credential_source_fingerprint
            self._pending_credential_updates = {}

        if self._pending_credential_updates is not None:
            pending_updates.update(self._pending_credential_updates)

        if pending_store is not None:
            persisted_pending = await pending_store.async_load()
            if (
                isinstance(persisted_pending, dict)
                and persisted_pending.get("source") == credential_source_fingerprint
                and persisted_pending.get("generation") == entry_generation
                and isinstance(persisted_pending.get("updates"), dict)
            ):
                pending_updates.update(persisted_pending["updates"])

        self._pending_credential_updates = pending_updates
        apply_credential_updates(
            client,
            {**credential_inputs, **pending_updates},
            fields,
        )

        if not any(provider.available for provider in providers):
            raise BadCredentialsException("No credentials available")

        result = await async_authenticate_with_providers_once(client, providers)
        credentials = {**submitted_credentials, **pending_updates}

        email = ""
        for bucket in result.data.updated_buckets:
            key = bucket.object_key
            if key.startswith("user."):
                email = bucket.value["email"]

        # Set unique id to user_id (object.key: user.xxxx)
        await self.async_set_unique_id(result.session.user)

        return ConfigFlowValidationResult(credentials, fields, email)

    async def _async_finish_reauthentication(
        self,
        data: dict[str, Any],
        credential_fields: frozenset[str],
    ) -> FlowResult:
        """Replace credentials without restoring state from the old login."""
        if self._config_entry is None:
            raise RuntimeError("Reauthentication entry is not available")

        previous_generation = self._config_entry.data.get(CONF_AUTH_GENERATION)
        credential_generation = uuid.uuid4().hex
        updated_data = {
            **{
                key: value
                for key, value in self._config_entry.data.items()
                if key not in credential_fields
            },
            **data,
            CONF_AUTH_GENERATION: credential_generation,
        }
        credentials = {
            key: updated_data[key] for key in credential_fields if key in updated_data
        }

        entry_data = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id)
        session_manager = getattr(entry_data, "session_manager", None)
        if session_manager is not None:
            await session_manager.async_stage_reauthentication(
                credential_generation,
                credentials,
            )
        else:
            store = Store(
                self.hass,
                STORAGE_VERSION,
                STORAGE_KEY_FORMAT.format(entry_id=self._config_entry.entry_id),
            )
            current_state = await store.async_load()
            await store.async_save(
                build_reauthentication_state(
                    current_state if isinstance(current_state, dict) else {},
                    credential_generation,
                    previous_generation,
                    credentials,
                )
            )

        self.hass.config_entries.async_update_entry(
            self._config_entry,
            data=updated_data,
        )
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(self._config_entry.entry_id)
        )
        pending_store = self._pending_reauthentication_store()
        if pending_store is not None:
            try:
                await pending_store.async_remove()
            except OSError:
                LOGGER.warning("Could not remove pending reauthentication data")
        return self.async_abort(reason="reauth_successful")

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
            if user_input["method"] == "extension":
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
                        }
                    ),
                }
            ),
            description_placeholders=DESCRIPTION_PLACEHOLDERS,
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
                    validated = await self.async_validate_input(validation_input)
                except TimeoutError, ClientError:
                    errors["base"] = "cannot_connect"
                except BadCredentialsException:
                    errors["base"] = "invalid_auth"
                except Exception as exception:  # pylint: disable=broad-except
                    errors["base"] = "unknown"
                    LOGGER.exception(exception)

            if not errors:
                data = {
                    **validated.credentials,
                    CONF_ACCOUNT_TYPE: self._default_account_type,
                }

                if self._config_entry:
                    return await self._async_finish_reauthentication(
                        data, validated.credential_fields
                    )

                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Nest Protect ({validated.email})", data=data
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
                    validated = await self.async_validate_input(user_input)
                except TimeoutError, ClientError:
                    errors["base"] = "cannot_connect"
                except BadCredentialsException:
                    errors["base"] = "invalid_auth"
                except Exception as exception:  # pylint: disable=broad-except
                    errors["base"] = "unknown"
                    LOGGER.exception(exception)

            if not errors:
                data = {
                    **validated.credentials,
                    CONF_ACCOUNT_TYPE: self._default_account_type,
                }
                if self._config_entry:
                    return await self._async_finish_reauthentication(
                        data, validated.credential_fields
                    )

                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"Nest Protect ({validated.email})", data=data
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
