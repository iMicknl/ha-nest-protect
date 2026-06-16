"""Nest session manager with persistence and three-tier auth fallback."""

from __future__ import annotations

import time

from homeassistant.helpers.storage import Store

from .const import (
    BACKOFF_INTERVALS,
    COOKIE_REFRESH_INTERVAL_SECONDS,
    LOGGER,
    MAX_AUTH_FAILURES,
    SESSION_EXPIRY_BUFFER_SECONDS,
)
from .debug_log import agent_debug_log
from .pynest.client import NestClient
from .pynest.exceptions import (
    BadCredentialsException,
    NotAuthenticatedException,
    PynestException,
)
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
        self._last_cookie_refresh: float = 0.0
        self._cookie_auth_failed: bool = False

    @property
    def refreshed_cookies(self) -> str | None:
        """Proxy to client's refreshed_cookies property."""
        return self._client.refreshed_cookies

    @property
    def cookie_auth_failed(self) -> bool:
        """Return True if the last Google cookie refresh failed."""
        return self._cookie_auth_failed

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
            # #region agent log
            agent_debug_log(
                "session.py:async_setup",
                "setup succeeded via persisted session tier",
                {"tier": 1},
                "H1",
            )
            # #endregion
            return nest_session

        # #region agent log
        agent_debug_log(
            "session.py:async_setup",
            "persisted session unavailable, using credential auth",
            {"tier": 2},
            "H1",
        )
        # #endregion
        return await self._async_authenticate_and_fetch()

    async def _async_try_persisted_session(self) -> FirstDataAPIResponse | None:
        """Attempt to restore and validate a persisted session.

        Returns FirstDataAPIResponse if the session is valid and accepted, None otherwise.
        """
        persisted = await self._store.async_load()

        if not persisted or not persisted.get("nest_session"):
            # #region agent log
            agent_debug_log(
                "session.py:_async_try_persisted_session",
                "no persisted session in store",
                {"has_persisted": bool(persisted)},
                "H1",
            )
            # #endregion
            return None

        restored_session = NestResponse.from_dict(persisted["nest_session"])

        if restored_session is None:
            return None

        if restored_session.is_expired(buffer_seconds=SESSION_EXPIRY_BUFFER_SECONDS):
            LOGGER.debug("Persisted session expired, falling through to cookie auth")
            # #region agent log
            agent_debug_log(
                "session.py:_async_try_persisted_session",
                "persisted session expired by timestamp",
                {"expires_in": restored_session.expires_in},
                "H1",
            )
            # #endregion
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
        except (NotAuthenticatedException, PynestException) as exc:  # fmt: skip
            LOGGER.debug(
                "Persisted session rejected by Nest, falling through to cookie auth"
            )
            # #region agent log
            agent_debug_log(
                "session.py:_async_try_persisted_session",
                "persisted session rejected by Nest API",
                {"error_type": type(exc).__name__},
                "H1",
            )
            # #endregion
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
            self._mark_cookie_refresh()
        elif self._client.refresh_token:
            auth = await self._client.get_access_token_from_refresh_token(
                self._client.refresh_token
            )
        else:
            # #region agent log
            agent_debug_log(
                "session.py:_async_authenticate_with_credentials",
                "no credentials available",
                {
                    "has_issue_token": bool(self._client.issue_token),
                    "has_cookies": bool(self._client.cookies),
                    "has_refresh_token": bool(self._client.refresh_token),
                },
                "H5",
            )
            # #endregion
            return None

        return await self._client.authenticate(auth.access_token)

    def _mark_cookie_refresh(self) -> None:
        """Record a successful Google cookie refresh."""
        self._last_cookie_refresh = time.monotonic()

    async def async_refresh_google_cookies(self) -> bool:
        """Refresh Google OAuth cookies via issueToken to keep them alive."""
        if not (self._client.issue_token and self._client.cookies):
            return False

        try:
            await self._client.get_access_token_from_cookies(
                self._client.issue_token, self._client.cookies
            )
        except BadCredentialsException:
            self._cookie_auth_failed = True
            # #region agent log
            agent_debug_log(
                "session.py:async_refresh_google_cookies",
                "proactive cookie refresh failed",
                {},
                "H2",
                run_id="post-fix",
            )
            # #endregion
            return False

        self._cookie_auth_failed = False
        self._mark_cookie_refresh()
        if self._client.refreshed_cookies:
            self._client.cookies = self._client.refreshed_cookies

        if self._client.auth:
            self._client.nest_session = await self._client.authenticate(
                self._client.auth.access_token
            )
            await self._async_persist(self._client.nest_session)

        # #region agent log
        agent_debug_log(
            "session.py:async_refresh_google_cookies",
            "proactive cookie refresh succeeded",
            {
                "cookies_changed": self._client.refreshed_cookies is not None,
                "nest_session_refreshed": bool(self._client.nest_session),
            },
            "H6",
            run_id="post-fix",
        )
        # #endregion
        return True

    async def maybe_refresh_google_cookies(self) -> bool | None:
        """Refresh Google cookies periodically even when Nest session is valid."""
        if not (self._client.issue_token and self._client.cookies):
            return None

        elapsed = time.monotonic() - self._last_cookie_refresh
        if self._last_cookie_refresh and elapsed < COOKIE_REFRESH_INTERVAL_SECONDS:
            return None

        return await self.async_refresh_google_cookies()

    async def ensure_session(self) -> None:
        """Ensure a valid Nest session exists, refreshing if needed."""
        await self.maybe_refresh_google_cookies()

        if self._client.nest_session and not self._client.nest_session.is_expired(
            buffer_seconds=SESSION_EXPIRY_BUFFER_SECONDS
        ):
            # #region agent log
            agent_debug_log(
                "session.py:ensure_session",
                "session still valid, skipping refresh",
                {"expires_in": self._client.nest_session.expires_in},
                "H2",
            )
            # #endregion
            return

        # #region agent log
        agent_debug_log(
            "session.py:ensure_session",
            "session refresh required",
            {
                "has_nest_session": bool(self._client.nest_session),
                "has_auth": bool(self._client.auth),
            },
            "H2",
        )
        # #endregion
        await self.async_refresh_session()

    async def async_refresh_session(self) -> None:
        """Force-refresh the Nest session via Google credentials."""
        if not self._client.auth or self._client.auth.is_expired():
            LOGGER.debug("Retrieving new Google access token")
            await self._client.get_access_token()
            if self._client.issue_token and self._client.cookies:
                self._mark_cookie_refresh()
            if self._client.refreshed_cookies:
                self._client.cookies = self._client.refreshed_cookies

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
