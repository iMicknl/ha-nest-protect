"""Nest session manager with persistence and three-tier auth fallback."""

from __future__ import annotations

import asyncio
import datetime
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

from homeassistant.helpers.storage import Store

from .const import (
    BACKOFF_INTERVALS,
    GOOGLE_THROTTLE_MAX_SECONDS,
    GOOGLE_THROTTLE_SECONDS,
    LOGGER,
    MAX_AUTH_FAILURES,
    SESSION_EXPIRY_BUFFER_SECONDS,
)
from .pynest.client import NestClient
from .pynest.exceptions import (
    GoogleThrottledException,
    NotAuthenticatedException,
    PynestException,
)
from .pynest.models import FirstDataAPIResponse, NestResponse


def _isoformat(timestamp: float) -> str | None:
    if not timestamp:
        return None
    return datetime.datetime.fromtimestamp(timestamp, datetime.UTC).isoformat()


def _backoff_for(consecutive: int) -> int:
    """Return the backoff interval in seconds for a consecutive failure count."""
    if consecutive == 0:
        return BACKOFF_INTERVALS[0]
    return BACKOFF_INTERVALS[min(consecutive - 1, len(BACKOFF_INTERVALS) - 1)]


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
        on_cookies_refreshed: Callable[[str], None] | None = None,
    ) -> None:
        """Initialize the session manager."""
        self._client = client
        self._store = store
        self._on_cookies_refreshed = on_cookies_refreshed
        self._consecutive_auth_failures: int = 0
        # Kept apart from auth failures so unrelated errors only slow polling
        # down and never push the user toward re-authentication.
        self._consecutive_errors: int = 0
        self._refresh_lock = asyncio.Lock()
        self._refresh_generation: int = 0
        # Wall-clock times; 0 means "never" / "not throttled".
        self._google_refreshed_at: float = 0.0
        self._google_throttled_until: float = 0.0
        self._google_stats: dict[str, Any] = {
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "throttled": 0,
            "last_error": None,
        }

    @property
    def refreshed_cookies(self) -> str | None:
        """Proxy to client's refreshed_cookies property."""
        return self._client.refreshed_cookies

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

    @property
    def google_refresh_diagnostics(self) -> dict[str, Any]:
        """Return Google credential refresh statistics for diagnostics."""
        return {
            **self._google_stats,
            "last_refreshed_at": _isoformat(self._google_refreshed_at),
            "throttled_until": _isoformat(self._google_throttled_until),
        }

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
            with self._report_rotated_cookies():
                auth = await self._async_track_google_request(
                    self._client.get_access_token_from_cookies(
                        self._client.issue_token, self._client.cookies
                    )
                )
        elif self._client.refresh_token:
            auth = await self._async_track_google_request(
                self._client.get_access_token_from_refresh_token(
                    self._client.refresh_token
                )
            )
        else:
            return None

        self._record_google_success()
        return await self._client.authenticate(auth.access_token)

    def _record_google_success(self) -> None:
        self._google_refreshed_at = time.time()
        self._google_stats["successes"] += 1

    async def _async_track_google_request[T](self, request: Awaitable[T]) -> T:
        """Await a Google token request, accounting for it and any throttling."""
        self._google_stats["attempts"] += 1
        try:
            return await request
        except GoogleThrottledException as err:
            pause = min(
                max(err.retry_after or 0, GOOGLE_THROTTLE_SECONDS),
                GOOGLE_THROTTLE_MAX_SECONDS,
            )
            self._google_throttled_until = time.time() + pause
            self._google_stats["throttled"] += 1
            self._google_stats["last_error"] = type(err).__name__
            LOGGER.warning(
                "Google is rate limiting credential refreshes; pausing them for %d minutes",
                pause // 60,
            )
            raise
        except Exception as err:
            self._google_stats["failures"] += 1
            self._google_stats["last_error"] = type(err).__name__
            raise

    async def _async_call_google(self) -> bool:
        """Refresh the Google access token. Caller must hold the lock.

        Returns True when a usable token came back.
        """
        LOGGER.debug("Retrieving new Google access token")
        with self._report_rotated_cookies():
            await self._async_track_google_request(self._client.get_access_token())

        if not self._client.auth or self._client.auth.is_expired():
            # No usable token came back — don't record a refresh that the
            # cookies never actually went through.
            self._google_stats["failures"] += 1
            self._google_stats["last_error"] = "no usable token"
            return False

        self._record_google_success()
        return True

    async def _async_ensure_google_token(self) -> None:
        """Make sure a valid Google token exists to authenticate against Nest.

        Reuses a valid in-memory token. Caller must hold the lock.
        """
        if self._client.auth and not self._client.auth.is_expired():
            return

        if time.time() < self._google_throttled_until:
            # Don't add to the load while Google is pushing back. The caller's
            # retry loop comes back here without reaching the network.
            raise GoogleThrottledException("Google credential refreshes are paused")

        await self._async_call_google()

    def _has_valid_session(self) -> bool:
        """Return True if the current Nest session is not (nearly) expired."""
        return bool(
            self._client.nest_session
            and not self._client.nest_session.is_expired(
                buffer_seconds=SESSION_EXPIRY_BUFFER_SECONDS
            )
        )

    async def ensure_session(self) -> None:
        """Ensure a valid Nest session exists, refreshing if needed."""
        if self._has_valid_session():
            return

        async with self._refresh_lock:
            if self._has_valid_session():
                return

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
            if self._refresh_generation != generation:
                return True

            return await self._async_refresh_session()

    async def _async_refresh_session(self) -> bool:
        """Refresh the Nest session; the caller must hold the refresh lock."""
        await self._async_ensure_google_token()

        if not self._client.auth:
            return False

        self._client.nest_session = await self._client.authenticate(
            self._client.auth.access_token
        )
        await self._async_persist(self._client.nest_session)
        self._refresh_generation += 1
        return True

    @contextmanager
    def _report_rotated_cookies(self) -> Iterator[None]:
        """Report cookies rotated by a Google token exchange, even if it fails.

        Google may already have invalidated the previous cookies, so they must be
        stored before any later auth step gets a chance to fail.
        """
        try:
            yield
        finally:
            if self._on_cookies_refreshed and self._client.refreshed_cookies:
                self._on_cookies_refreshed(self._client.refreshed_cookies)

    async def _async_persist(self, nest_session: NestResponse) -> None:
        """Save Nest session to store for reuse across restarts."""
        await self._store.async_save(
            {
                "nest_session": nest_session.to_dict(),
                "transport_url": self._client.transport_url,
            }
        )
