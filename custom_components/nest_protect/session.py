"""Nest session manager with persistence and three-tier auth fallback."""

from __future__ import annotations

from homeassistant.helpers.storage import Store

from .const import (
    BACKOFF_INTERVALS,
    LOGGER,
    MAX_AUTH_FAILURES,
    SESSION_EXPIRY_BUFFER_SECONDS,
)
from .pynest.client import NestClient
from .pynest.exceptions import NotAuthenticatedException, PynestException
from .pynest.models import FirstDataAPIResponse, NestResponse


class NestSessionManager:
    """Manage Nest session lifecycle: persist, restore, and authenticate.

    Three-tier auth fallback on startup:
    1. Reuse persisted Nest session if still valid (skip Google entirely)
    2. Re-authenticate with Google using stored cookies/refresh_token
    3. Return None (caller should raise ConfigEntryAuthFailed)
    """

    def __init__(
        self,
        client: NestClient,
        store: Store,
    ) -> None:
        """Initialize the session manager."""
        self._client = client
        self._store = store
        self._consecutive_failures: int = 0

    @property
    def refreshed_cookies(self) -> str | None:
        """Proxy to client's refreshed_cookies property."""
        return self._client.refreshed_cookies

    @property
    def consecutive_failures(self) -> int:
        """Return the number of consecutive failures."""
        return self._consecutive_failures

    @property
    def should_trigger_reauth(self) -> bool:
        """Return True if failures exceed the threshold."""
        return self._consecutive_failures >= MAX_AUTH_FAILURES

    @property
    def backoff_interval(self) -> int:
        """Return the current backoff interval in seconds."""
        if self._consecutive_failures == 0:
            return BACKOFF_INTERVALS[0]
        idx = min(self._consecutive_failures - 1, len(BACKOFF_INTERVALS) - 1)
        return BACKOFF_INTERVALS[idx]

    def record_failure(self) -> None:
        """Record a consecutive failure."""
        self._consecutive_failures += 1

    def record_success(self) -> None:
        """Reset failure counter on success."""
        self._consecutive_failures = 0

    async def async_setup(self) -> FirstDataAPIResponse | None:
        """Set up authentication using three-tier fallback.

        Returns FirstDataAPIResponse on success, None if no credentials available.
        """
        nest_session = await self._async_try_persisted_session()

        if nest_session is not None:
            return nest_session

        return await self._async_authenticate_and_fetch()

    async def _async_try_persisted_session(self) -> FirstDataAPIResponse | None:
        """Attempt to restore and validate a persisted session.

        Returns FirstDataAPIResponse if the session is valid and accepted, None otherwise.
        """
        persisted = await self._store.async_load()

        if not persisted or not persisted.get("nest_session"):
            return None

        restored_session = NestResponse.from_dict(persisted["nest_session"])

        if restored_session is None:
            return None

        if restored_session.is_expired(buffer_seconds=SESSION_EXPIRY_BUFFER_SECONDS):
            LOGGER.debug("Persisted session expired, falling through to cookie auth")
            return None

        LOGGER.debug(
            "Reusing persisted Nest session (expires: %s)",
            restored_session.expires_in,
        )
        self._client.nest_session = restored_session
        self._client.transport_url = persisted.get("transport_url")

        # Validate the session is actually accepted by Nest
        try:
            return await self._client.get_first_data(
                restored_session.access_token, restored_session.userid
            )
        except (NotAuthenticatedException, PynestException):  # fmt: skip
            LOGGER.debug(
                "Persisted session rejected by Nest, falling through to cookie auth"
            )
            self._client.nest_session = None
            return None

    async def _async_authenticate_and_fetch(self) -> FirstDataAPIResponse | None:
        """Authenticate with credentials and fetch first data.

        Returns FirstDataAPIResponse on success, None if no credentials available.
        """
        nest_response = await self._async_authenticate_with_credentials()

        if nest_response is None:
            return None

        self._client.nest_session = nest_response
        await self._async_persist(nest_response)

        return await self._client.get_first_data(
            nest_response.access_token, nest_response.userid
        )

    async def _async_authenticate_with_credentials(self) -> NestResponse | None:
        """Authenticate using cookies or refresh_token.

        Returns a NestResponse on success, None if no credentials are available.
        Raises authentication exceptions from the underlying client on failure.
        """
        if self._client.issue_token and self._client.cookies:
            auth = await self._client.get_access_token_from_cookies(
                self._client.issue_token, self._client.cookies
            )
        elif self._client.refresh_token:
            auth = await self._client.get_access_token_from_refresh_token(
                self._client.refresh_token
            )
        else:
            return None

        return await self._client.authenticate(auth.access_token)

    async def ensure_session(self) -> None:
        """Ensure a valid Nest session exists, refreshing if needed."""
        if self._client.nest_session and not self._client.nest_session.is_expired(
            buffer_seconds=SESSION_EXPIRY_BUFFER_SECONDS
        ):
            return

        await self.async_refresh_session()

    async def async_refresh_session(self) -> None:
        """Force-refresh the Nest session via Google credentials."""
        if not self._client.auth or self._client.auth.is_expired():
            LOGGER.debug("Retrieving new Google access token")
            await self._client.get_access_token()

        if self._client.auth:
            self._client.nest_session = await self._client.authenticate(
                self._client.auth.access_token
            )
            await self._async_persist(self._client.nest_session)

    async def _async_persist(self, nest_session: NestResponse) -> None:
        """Save Nest session to store for reuse across restarts."""
        await self._store.async_save(
            {
                "nest_session": nest_session.to_dict(),
                "transport_url": self._client.transport_url,
            }
        )
