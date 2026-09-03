"""Nest session manager with persistence and three-tier auth fallback."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from homeassistant.helpers.storage import Store

from .const import (
    BACKOFF_INTERVALS,
    GOOGLE_REFRESH_INTERVAL_SECONDS,
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
        self._google_refreshed_at: float = 0.0
        self._refresh_lock = asyncio.Lock()

        # Invoked after Google credentials are refreshed, so the caller can
        # persist the cookies Google rotated during it. Every refresh path
        # runs through here, which the individual call sites did not.
        self.on_credentials_refreshed: Callable[[], None] | None = None

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

        stored_refreshed_at = persisted.get("google_refreshed_at")
        self._google_refreshed_at = self._coerce_refresh_time(stored_refreshed_at)

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
            first_data = await self._client.get_first_data(
                restored_session.access_token, restored_session.userid
            )
        except (NotAuthenticatedException, PynestException):  # fmt: skip
            LOGGER.debug(
                "Persisted session rejected by Nest, falling through to cookie auth"
            )
            self._client.nest_session = None
            return None

        if stored_refreshed_at != self._google_refreshed_at:
            # Write the assumed clock straight back, otherwise every restart
            # would assume "fresh" again and could postpone the refresh
            # indefinitely on a frequently restarted instance.
            await self._async_persist(restored_session)

        return first_data

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
            try:
                auth = await self._client.get_access_token_from_cookies(
                    self._client.issue_token, self._client.cookies
                )
            finally:
                # Notify here rather than after async_setup() returns: Google
                # may already have rotated the cookies, even if the request
                # then failed, and a retry must not start from the superseded
                # set.
                self._notify_credentials_refreshed()
        elif self._client.refresh_token:
            auth = await self._client.get_access_token_from_refresh_token(
                self._client.refresh_token
            )
        else:
            return None

        self._google_refreshed_at = time.time()

        return await self._client.authenticate(auth.access_token)

    @staticmethod
    def _coerce_refresh_time(value: object) -> float:
        """Normalise a persisted refresh timestamp into a usable value."""
        now = time.time()

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            # Missing (store predates this key) or corrupt. Assume the cookies
            # are fresh so the "skip Google entirely" startup path survives;
            # the caller writes this assumption back to make it durable.
            return now

        if value > now:
            # A future timestamp — clock skew or a corrupt store — must not
            # suppress the refresh until the wall clock catches up.
            return now

        return float(value)

    async def ensure_google_credentials(self, *, force: bool = False) -> bool:
        """Refresh the Google access token when it is due.

        Deliberately independent of Nest session validity. Google rotates the
        auth cookies on every token refresh and only honours the previous
        values for a limited grace window, so the stored cookies have to be
        exercised on their own cadence. Gating this on the Nest session instead
        leaves them untouched for that session's full lifetime — weeks — by
        which point Google has moved on and invalidates the session
        (USER_LOGGED_OUT) the next time they are presented.

        Records the refresh time in memory; ensure_session() and
        async_refresh_session() are what write it to the store alongside the
        Nest session. force skips the interval but still reuses a valid
        in-memory token; it is for the paths recovering from a rejected
        session.

        Returns True when the credentials were actually refreshed.
        """
        try:
            async with self._refresh_lock:
                return await self._async_refresh_google_credentials(force=force)
        finally:
            self._notify_credentials_refreshed()

    async def _async_refresh_google_credentials(self, *, force: bool) -> bool:
        """Refresh the Google access token. Caller must hold the lock."""
        if self._client.auth:
            if not self._client.auth.is_expired():
                return False
        elif (
            not force
            and 0
            <= (time.time() - self._google_refreshed_at)
            < GOOGLE_REFRESH_INTERVAL_SECONDS
        ):
            # No token in memory yet, but the cookies were exercised recently
            # enough for a restored session to keep skipping Google. A negative
            # elapsed time means the clock went backwards, which must not
            # suppress the refresh until it catches up.
            return False

        LOGGER.debug("Retrieving new Google access token")
        await self._client.get_access_token()

        if not self._client.auth or self._client.auth.is_expired():
            # No usable token came back — don't record a refresh that the
            # cookies never actually went through.
            return False

        self._google_refreshed_at = time.time()

        return True

    def _notify_credentials_refreshed(self) -> None:
        """Let the caller persist cookies Google rotated during a refresh.

        Keyed on the cookies actually captured rather than on the token
        request succeeding: the client applies Set-Cookie before it reads the
        response body, so a body that fails or is cancelled still leaves the
        rotated set authoritative and the superseded one unusable.

        Called outside the lock, and from a finally block — the callback
        reaches into Home Assistant to update the config entry, so it has no
        business holding up other refreshes.
        """
        if self._client.refreshed_cookies and self.on_credentials_refreshed:
            self.on_credentials_refreshed()

    async def ensure_session(self) -> None:
        """Ensure valid Google credentials and a valid Nest session."""
        refreshed = False
        try:
            # Serialised as a whole: the subscriber, entities and the lock
            # observer share one manager. Concurrent refreshes would each
            # rotate the cookies while only the last response survives —
            # losing the value Google now expects — and would authenticate
            # against Nest twice over.
            async with self._refresh_lock:
                # Keep the Google cookies warm even while the Nest session is
                # still valid — Nest issues sessions lasting weeks, far longer
                # than Google honours a given cookie set.
                refreshed = await self._async_refresh_google_credentials(force=False)

                # Re-read validity now the lock is held: a caller that waited
                # here may find the session it was about to replace renewed.
                nest_session_valid = self._client.nest_session is not None and not (
                    self._client.nest_session.is_expired(
                        buffer_seconds=SESSION_EXPIRY_BUFFER_SECONDS
                    )
                )

                if not nest_session_valid:
                    # Persists the session and the refresh clock together.
                    await self._async_refresh_nest_session()
                elif refreshed:
                    await self._async_persist(self._client.nest_session)
        finally:
            self._notify_credentials_refreshed()

    async def async_refresh_session(self) -> bool:
        """Force-refresh the Nest session via Google credentials.

        Returns True when a fresh Nest session was obtained and persisted.
        """
        try:
            async with self._refresh_lock:
                await self._async_refresh_google_credentials(force=True)
                return await self._async_refresh_nest_session()
        finally:
            self._notify_credentials_refreshed()

    async def _async_refresh_nest_session(self) -> bool:
        """Re-authenticate against Nest and persist. Caller must hold the lock."""
        if not self._client.auth:
            return False

        self._client.nest_session = await self._client.authenticate(
            self._client.auth.access_token
        )
        await self._async_persist(self._client.nest_session)
        return True

    async def _async_persist(self, nest_session: NestResponse) -> None:
        """Save Nest session to store for reuse across restarts."""
        await self._store.async_save(
            {
                "nest_session": nest_session.to_dict(),
                "transport_url": self._client.transport_url,
                "google_refreshed_at": self._google_refreshed_at,
            }
        )
