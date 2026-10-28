"""Serialized Nest session lifecycle and credential recovery."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError
from homeassistant.helpers.storage import Store

from .const import (
    AUTH_RETRY_DELAYS,
    BACKOFF_INTERVALS,
    CONF_AUTH_GENERATION,
    CONF_PREVIOUS_AUTH_GENERATION,
    LOGGER,
    MAX_AUTH_FAILURES,
    SESSION_EXPIRY_BUFFER_SECONDS,
)
from .credentials import (
    CredentialProvider,
    CredentialUpdateCallback,
    apply_credential_updates,
    create_credential_providers,
    credential_field_names,
)
from .pynest.client import NestClient
from .pynest.exceptions import (
    BadCredentialsException,
    NestServiceException,
    NotAuthenticatedException,
    PynestException,
)
from .pynest.models import FirstDataAPIResponse, NestResponse

_AUTH_SCOPED_STORE_FIELDS = frozenset(
    {
        CONF_AUTH_GENERATION,
        CONF_PREVIOUS_AUTH_GENERATION,
        "credentials",
        "nest_session",
        "transport_url",
    }
)


def _backoff_for(consecutive: int) -> int:
    """Return the backoff interval in seconds for a consecutive failure count."""
    if consecutive == 0:
        return BACKOFF_INTERVALS[0]
    return BACKOFF_INTERVALS[min(consecutive - 1, len(BACKOFF_INTERVALS) - 1)]


def build_reauthentication_state(
    current_state: Mapping[str, Any],
    credential_generation: str,
    previous_generation: str | None,
    credentials: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a handoff while preserving unrelated future Store metadata."""
    state = {
        key: value
        for key, value in current_state.items()
        if key not in _AUTH_SCOPED_STORE_FIELDS
    }
    state.update(
        {
            CONF_AUTH_GENERATION: credential_generation,
            CONF_PREVIOUS_AUTH_GENERATION: previous_generation,
            "credentials": dict(credentials),
        }
    )
    return state


@dataclass(frozen=True)
class ProviderAuthenticationResult:
    """A Nest session accepted by the complete provider pipeline."""

    provider_name: str
    session: NestResponse
    data: FirstDataAPIResponse


async def async_authenticate_with_providers_once(
    client: NestClient,
    providers: Sequence[CredentialProvider],
) -> ProviderAuthenticationResult:
    """Try one complete, validated authentication per available provider."""
    last_bad_credentials: BadCredentialsException | None = None
    last_session_rejection: NotAuthenticatedException | None = None
    last_transient_failure: Exception | None = None

    for provider in providers:
        if not provider.available:
            continue

        LOGGER.debug("Authenticating with %s credentials", provider.name)
        session: NestResponse | None = None
        validated = False
        try:
            auth = await provider.async_get_access_token()
            session = await client.authenticate(auth.access_token)
            client.nest_session = session
            data = await client.get_first_data(session.access_token, session.userid)
        except BadCredentialsException as exception:
            last_bad_credentials = exception
        except NotAuthenticatedException as exception:
            last_session_rejection = exception
        except (
            TimeoutError,
            ClientError,
            NestServiceException,
            PynestException,
        ) as exception:
            last_transient_failure = exception
        else:
            validated = True
            return ProviderAuthenticationResult(provider.name, session, data)
        finally:
            # NestClient installs a new session before app_launch validates it.
            if not validated and session is not None:
                client.nest_session = None

    if last_transient_failure is not None:
        raise last_transient_failure
    if last_session_rejection is not None:
        raise last_session_rejection
    if last_bad_credentials is not None:
        raise last_bad_credentials
    raise RuntimeError("No credential provider produced an authentication result")


class NestSessionManager:
    """Own Google credential exchange and the resulting Nest session."""

    def __init__(
        self,
        client: NestClient,
        store: Store,
        *,
        credential_generation: str | None = None,
        credential_update_callback: CredentialUpdateCallback | None = None,
        credential_providers: Sequence[CredentialProvider] | None = None,
        initial_credentials: Mapping[str, Any] | None = None,
        retry_delays: Sequence[float] = AUTH_RETRY_DELAYS,
    ) -> None:
        """Initialize the session manager."""
        self._client = client
        self._store = store
        self._credential_generation = credential_generation
        self._credential_update_callback = credential_update_callback
        self._retry_delays = tuple(retry_delays)
        self._consecutive_auth_failures: int = 0
        # Kept apart from auth failures so unrelated errors only slow polling
        # down and never push the user toward re-authentication.
        self._consecutive_errors: int = 0
        self._refresh_lock = asyncio.Lock()
        self._refresh_generation: int = 0
        self._stored_state: dict[str, Any] = {}
        self._state_loaded = False
        self._stored_credentials_applied = False
        self._retired = False
        self._credential_providers = tuple(
            credential_providers
            if credential_providers is not None
            else create_credential_providers(client, self._async_save_credentials)
        )
        self._credential_fields = credential_field_names(self._credential_providers)
        if initial_credentials:
            apply_credential_updates(
                self._client, initial_credentials, self._credential_fields
            )

    @property
    def consecutive_auth_failures(self) -> int:
        """Return the number of consecutive authentication failures."""
        return self._consecutive_auth_failures

    @property
    def consecutive_errors(self) -> int:
        """Return the number of consecutive non-authentication errors."""
        return self._consecutive_errors

    @property
    def should_trigger_reauth(self) -> bool:
        """Return True if authentication failures exceed the threshold."""
        return self._consecutive_auth_failures >= MAX_AUTH_FAILURES

    @property
    def backoff_interval(self) -> int:
        """Return the backoff interval in seconds after an auth failure."""
        return _backoff_for(self._consecutive_auth_failures)

    @property
    def error_backoff_interval(self) -> int:
        """Return the backoff interval in seconds after a non-auth error."""
        return _backoff_for(self._consecutive_errors)

    def record_auth_failure(self) -> None:
        """Record a consecutive authentication failure."""
        self._consecutive_auth_failures += 1

    def record_error(self) -> None:
        """Record a consecutive non-authentication error."""
        self._consecutive_errors += 1

    def record_auth_success(self) -> None:
        """Reset the auth failure counter once Nest accepts the session."""
        self._consecutive_auth_failures = 0

    def record_success(self) -> None:
        """Reset both counters after a fully successful update."""
        self._consecutive_auth_failures = 0
        self._consecutive_errors = 0

    async def async_setup(self) -> FirstDataAPIResponse | None:
        """Restore a validated session or create one from configured providers.

        Returns None when no credentials are configured.
        """
        async with self._refresh_lock:
            self._raise_if_retired()
            await self._async_prepare_state()

            if data := await self._async_try_persisted_session():
                return data

            result = await self._async_authenticate_with_providers()
            if result is None:
                return None

            _, data = result
            return data

    def _has_valid_session(self) -> bool:
        """Return True if the current Nest session is not (nearly) expired."""
        return self._session_is_valid(self._client.nest_session)

    async def ensure_session(self) -> None:
        """Ensure a valid Nest session exists, refreshing if needed."""
        if self._has_valid_session():
            return

        async with self._refresh_lock:
            self._raise_if_retired()
            if self._has_valid_session():
                return

            await self._async_prepare_state()
            await self._async_clear_persisted_session()
            await self._async_refresh_session()

    async def async_run_command[T](
        self,
        command: Callable[[], Awaitable[T]],
        auth_errors: type[Exception] | tuple[type[Exception], ...],
    ) -> T:
        """Run a command, refreshing the session and retrying once on auth errors.

        A session can be revoked before its advertised expiry, so a valid-looking
        session is no guarantee the command will be accepted.
        """
        await self.ensure_session()
        try:
            return await command()
        except auth_errors:
            LOGGER.debug("Command rejected by Nest, refreshing session and retrying")
            if not await self.async_refresh_session():
                raise
        return await command()

    async def async_refresh_session(self) -> bool:
        """Force-refresh the Nest session via Google credentials.

        Returns True when a fresh Nest session was obtained and persisted.
        """
        # Callers force a refresh because Nest rejected the current session, so
        # expiry can't tell whether it is still stale. Session identity can't
        # either: it is swapped before persisting, which may still fail. Only a
        # refresh that fully completed while this one waited counts.
        generation = self._refresh_generation

        async with self._refresh_lock:
            self._raise_if_retired()
            if self._refresh_generation != generation:
                return True

            await self._async_prepare_state()
            # The cached Google token created the rejected session, so reusing
            # it could hand back a session Nest rejects again.
            self._client.auth = None
            self._client.nest_session = None
            await self._async_clear_persisted_session()
            return await self._async_refresh_session()

    async def async_stage_reauthentication(
        self,
        credential_generation: str,
        credentials: Mapping[str, Any],
    ) -> None:
        """Retire this manager after durably staging replacement credentials."""
        async with self._refresh_lock:
            await self._async_load_state()
            previous_generation = self._credential_generation
            staged_state = build_reauthentication_state(
                self._stored_state,
                credential_generation,
                previous_generation,
                credentials,
            )
            await self._store.async_save(staged_state)

            self._retired = True
            self._credential_generation = credential_generation
            self._client.auth = None
            self._client.nest_session = None
            self._stored_state = staged_state
            self._state_loaded = True
            self._stored_credentials_applied = True

    async def _async_refresh_session(self) -> bool:
        """Refresh the Nest session; the caller must hold the refresh lock."""
        auth = self._client.auth
        if auth and not auth.is_expired():
            session: NestResponse | None = None
            validated = False
            try:
                session = await self._client.authenticate(auth.access_token)
                await self._client.get_first_data(session.access_token, session.userid)
            except NotAuthenticatedException:
                self._client.auth = None
            else:
                validated = True
                self._client.nest_session = session
                await self._async_persist_session(session)
                self._refresh_generation += 1
                return True
            finally:
                if not validated and session is not None:
                    self._client.nest_session = None

        result = await self._async_authenticate_with_providers()
        if result is None:
            return False
        self._refresh_generation += 1
        return True

    async def _async_authenticate_with_providers(
        self,
    ) -> tuple[NestResponse, FirstDataAPIResponse] | None:
        """Try every provider, retrying only explicit authentication rejection."""
        if not any(provider.available for provider in self._credential_providers):
            return None

        last_bad_credentials: BadCredentialsException | None = None
        last_session_rejection: NotAuthenticatedException | None = None

        for attempt in range(len(self._retry_delays) + 1):
            try:
                result = await async_authenticate_with_providers_once(
                    self._client, self._credential_providers
                )
            except BadCredentialsException as exception:
                last_bad_credentials = exception
            except NotAuthenticatedException as exception:
                last_session_rejection = exception
            else:
                await self._async_persist_session(result.session)
                return result.session, result.data

            if attempt < len(self._retry_delays):
                delay = self._retry_delays[attempt]
                LOGGER.debug(
                    "Authentication attempt %d rejected; retrying in %ss",
                    attempt + 1,
                    delay,
                )
                await asyncio.sleep(delay)

        # Google accepted the credentials if Nest got far enough to reject the
        # session, so that must not be reported as bad credentials.
        if last_session_rejection is not None:
            raise last_session_rejection

        if last_bad_credentials is None:
            raise RuntimeError("Authentication attempts ended without a result")

        raise last_bad_credentials

    async def _async_try_persisted_session(
        self,
    ) -> FirstDataAPIResponse | None:
        """Validate a non-expired session from durable state."""
        persisted_session = self._stored_state.get("nest_session")
        if not persisted_session:
            return None

        restored_session = NestResponse.from_dict(persisted_session)
        if restored_session is None or not self._session_is_valid(restored_session):
            LOGGER.debug("Persisted session expired, falling through to credentials")
            await self._async_clear_persisted_session()
            return None

        LOGGER.debug(
            "Reusing persisted Nest session (expires: %s)",
            restored_session.expires_in,
        )
        self._client.nest_session = restored_session
        self._client.transport_url = self._stored_state.get("transport_url")

        # Only an explicit rejection falls through: a transient failure would
        # hit the credentials too and must not count against them.
        validated = False
        try:
            data = await self._client.get_first_data(
                restored_session.access_token, restored_session.userid
            )
        except NotAuthenticatedException:
            LOGGER.debug(
                "Persisted session rejected by Nest, falling through to credentials"
            )
            await self._async_clear_persisted_session()
            return None
        else:
            validated = True
            return data
        finally:
            if not validated:
                self._client.nest_session = None

    async def _async_prepare_state(self) -> None:
        """Load durable state and apply newer stored credentials once."""
        await self._async_load_state()
        await self._async_prepare_credential_generation()
        if self._stored_credentials_applied:
            return

        self._stored_credentials_applied = True
        updates = self._stored_state.get("credentials")
        if not isinstance(updates, dict) or not updates:
            return

        apply_credential_updates(self._client, updates, self._credential_fields)
        if self._credential_update_callback:
            await self._credential_update_callback(
                self._with_credential_generation(updates)
            )

    async def _async_prepare_credential_generation(self) -> None:
        """Reconcile durable state with the config entry's login generation."""
        stored_generation = self._stored_state.get(CONF_AUTH_GENERATION)
        previous_generation_present = (
            CONF_PREVIOUS_AUTH_GENERATION in self._stored_state
        )
        previous_generation = self._stored_state.get(CONF_PREVIOUS_AUTH_GENERATION)

        if self._credential_generation is None:
            self._credential_generation = stored_generation or uuid.uuid4().hex
            if stored_generation is None:
                self._stored_state[CONF_AUTH_GENERATION] = self._credential_generation
                await self._store.async_save(self._stored_state)

            if self._credential_update_callback:
                await self._credential_update_callback(
                    {CONF_AUTH_GENERATION: self._credential_generation}
                )
            return

        if stored_generation == self._credential_generation:
            return

        if (
            previous_generation_present
            and previous_generation == self._credential_generation
            and isinstance(stored_generation, str)
        ):
            self._credential_generation = stored_generation
            if self._credential_update_callback:
                await self._credential_update_callback(
                    {
                        CONF_AUTH_GENERATION: stored_generation,
                        CONF_PREVIOUS_AUTH_GENERATION: previous_generation,
                    }
                )
            return

        self._stored_state = {
            key: value
            for key, value in self._stored_state.items()
            if key not in _AUTH_SCOPED_STORE_FIELDS
        }
        self._stored_state[CONF_AUTH_GENERATION] = self._credential_generation
        await self._store.async_save(self._stored_state)

    async def _async_load_state(self) -> None:
        """Load the Store once."""
        if self._state_loaded:
            return

        stored = await self._store.async_load()
        self._stored_state = dict(stored) if stored else {}
        self._state_loaded = True

    async def _async_save_credentials(self, updates: Mapping[str, Any]) -> None:
        """Durably save provider changes before dependent requests continue.

        Google may already have invalidated the previous cookies, so they must be
        stored before any later auth step gets a chance to fail.
        """
        await self._async_load_state()
        credentials = dict(self._stored_state.get("credentials", {}))
        changed = any(
            key not in credentials or credentials[key] != value
            for key, value in updates.items()
        )
        if not changed:
            return

        credentials.update(updates)
        self._stored_state["credentials"] = credentials
        await self._store.async_save(self._stored_state)
        apply_credential_updates(self._client, updates, self._credential_fields)

        if self._credential_update_callback:
            await self._credential_update_callback(
                self._with_credential_generation(updates)
            )

    async def _async_persist_session(self, session: NestResponse) -> None:
        """Persist a usable Nest session without dropping credential state."""
        self._stored_state["nest_session"] = session.to_dict()
        self._stored_state["transport_url"] = self._client.transport_url
        await self._store.async_save(self._stored_state)

    async def _async_clear_persisted_session(self) -> None:
        """Remove a rejected or expired Nest session from durable state."""
        removed = self._stored_state.pop("nest_session", None) is not None
        removed = self._stored_state.pop("transport_url", None) is not None or removed
        if removed:
            await self._store.async_save(self._stored_state)

    def _with_credential_generation(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        """Tag config-entry updates so a replaced manager cannot overwrite them."""
        if self._credential_generation is None:
            raise RuntimeError("Credential generation has not been prepared")
        return {**updates, CONF_AUTH_GENERATION: self._credential_generation}

    def _raise_if_retired(self) -> None:
        """Prevent a manager replaced by reauthentication from writing again."""
        if self._retired:
            raise NotAuthenticatedException("Authentication generation was replaced")

    @staticmethod
    def _session_is_valid(session: NestResponse | None) -> bool:
        """Return whether a session is present outside its expiry buffer."""
        if session is None:
            return False
        try:
            return not session.is_expired(buffer_seconds=SESSION_EXPIRY_BUFFER_SECONDS)
        except TypeError, ValueError:
            return False
