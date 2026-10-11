"""Nest session manager with persistence and three-tier auth fallback."""

from __future__ import annotations

import asyncio
import datetime
import random
import time
from collections.abc import Callable
from typing import Any

from homeassistant.helpers.storage import Store

from .const import (
    BACKOFF_INTERVALS,
    GOOGLE_REFRESH_INTERVAL_SECONDS,
    GOOGLE_REFRESH_JITTER_SECONDS,
    GOOGLE_REFRESH_RETRY_INTERVALS,
    GOOGLE_THROTTLE_MAX_SECONDS,
    GOOGLE_THROTTLE_SECONDS,
    LOGGER,
    MAX_AUTH_FAILURES,
    SESSION_EXPIRY_BUFFER_SECONDS,
)
from .pynest.client import NestClient
from .pynest.exceptions import (
    BadCredentialsException,
    GoogleThrottledException,
    NotAuthenticatedException,
    PynestException,
)
from .pynest.models import FirstDataAPIResponse, NestResponse


def _isoformat(timestamp: float) -> str | None:
    if not timestamp:
        return None
    return datetime.datetime.fromtimestamp(timestamp, datetime.UTC).isoformat()


class NestSessionManager:
    """Manage Nest session lifecycle: persist, restore, and authenticate.

    Three-tier auth fallback on startup:
    1. Reuse persisted Nest session if still valid (skip Google entirely)
    2. Re-authenticate with Google using stored cookies/refresh_token
    3. Return None (caller should raise ConfigEntryAuthFailed)

    While the Nest session is valid, the Google credentials are still exercised
    roughly hourly ("kept warm"), best effort, so the cookies don't go stale.
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
        self._refresh_lock = asyncio.Lock()
        self._google_refreshed_at: float = 0.0
        # Wall-clock times; 0 means "not scheduled" / "not throttled".
        self._google_next_refresh_at: float = 0.0
        self._google_throttled_until: float = 0.0
        self._google_retry_failures: int = 0
        self._keep_warm_disabled: bool = False
        self._google_stats: dict[str, Any] = {
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "throttled": 0,
            "last_error": None,
        }
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

    @property
    def google_refresh_diagnostics(self) -> dict[str, Any]:
        """Return Google credential refresh statistics for diagnostics."""
        return {
            **self._google_stats,
            "last_refreshed_at": _isoformat(self._google_refreshed_at),
            "next_refresh_at": _isoformat(self._google_next_refresh_at),
            "throttled_until": _isoformat(self._google_throttled_until),
            "keep_warm_disabled": self._keep_warm_disabled,
        }

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
        self._schedule_keep_warm()

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

        self._record_google_success()
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

    def _schedule_keep_warm(self) -> None:
        """Schedule the next keep-warm refresh from the last successful one."""
        # Jitter spreads installs out so they don't converge on the same
        # minute, e.g. after a Home Assistant release has everyone restart.
        jitter = random.uniform(  # noqa: S311
            -GOOGLE_REFRESH_JITTER_SECONDS, GOOGLE_REFRESH_JITTER_SECONDS
        )
        self._google_next_refresh_at = (
            self._google_refreshed_at + GOOGLE_REFRESH_INTERVAL_SECONDS + jitter
        )

    def _record_google_success(self) -> None:
        self._google_refreshed_at = time.time()
        self._google_retry_failures = 0
        self._google_stats["successes"] += 1
        self._schedule_keep_warm()

    def _keep_warm_due(self) -> bool:
        if self._keep_warm_disabled:
            return False
        now = time.time()
        if now < self._google_throttled_until:
            return False
        # A clock that went backwards must not suppress a due refresh.
        return now >= self._google_next_refresh_at or now < self._google_refreshed_at

    async def _async_call_google(self) -> bool:
        """Refresh the Google access token. Caller must hold the lock.

        Returns True when a usable token came back.
        """
        LOGGER.debug("Retrieving new Google access token")
        self._google_stats["attempts"] += 1
        try:
            await self._client.get_access_token()
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

        if not self._client.auth or self._client.auth.is_expired():
            # No usable token came back — don't record a refresh that the
            # cookies never actually went through.
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

    async def _async_keep_google_credentials_warm(self) -> bool:
        """Exercise the Google credentials while the Nest session is still valid.

        Google rotates the auth cookies and only honours superseded values for
        a limited grace window, while Nest issues sessions lasting weeks, so
        the cookies have to be exercised on their own cadence. This is best
        effort: the Nest session still works, so a failure here must not
        interrupt updates or commands. It is retried on its own backoff.
        Caller must hold the lock. Returns True when the refresh succeeded.
        """
        try:
            return await self._async_call_google()
        except BadCredentialsException:
            # Retrying dead credentials every hour only adds load; the next
            # Nest re-authentication surfaces this as a reauth request.
            self._keep_warm_disabled = True
            LOGGER.warning(
                "Google rejected the stored credentials. Updates continue until "
                "the current Nest session expires (%s); re-authenticate the "
                "integration before then to avoid an interruption",
                self._client.nest_session.expires_in,
            )
        except Exception as err:  # noqa: BLE001 - any failure is retried later
            self._google_retry_failures += 1
            idx = min(self._google_retry_failures, len(GOOGLE_REFRESH_RETRY_INTERVALS))
            delay = GOOGLE_REFRESH_RETRY_INTERVALS[idx - 1] * random.uniform(  # noqa: S311
                0.8, 1.2
            )
            self._google_next_refresh_at = time.time() + delay
            LOGGER.debug(
                "Refreshing Google credentials failed (%r); retrying in %d minutes",
                err,
                delay // 60,
            )
        return False

    def _notify_credentials_refreshed(self) -> None:
        """Let the caller persist cookies Google rotated during a refresh.

        Keyed on the cookies actually captured rather than on the token
        request succeeding: the client applies rotated Set-Cookie values before
        it reads the response body, so a body that fails or is cancelled still
        leaves the rotated set authoritative and the superseded one unusable.

        Called outside the lock, and from a finally block — the callback
        reaches into Home Assistant to update the config entry, so it has no
        business holding up other refreshes.
        """
        if self._client.refreshed_cookies and self.on_credentials_refreshed:
            self.on_credentials_refreshed()

    def _nest_session_valid(self) -> bool:
        return self._client.nest_session is not None and not (
            self._client.nest_session.is_expired(
                buffer_seconds=SESSION_EXPIRY_BUFFER_SECONDS
            )
        )

    async def ensure_session(self) -> None:
        """Ensure a valid Nest session, keeping the Google credentials warm."""
        try:
            # Serialised as a whole: the subscriber, entities and the lock
            # observer share one manager. Concurrent refreshes would each
            # rotate the cookies while only the last response survives —
            # losing the value Google now expects — and would authenticate
            # against Nest twice over.
            async with self._refresh_lock:
                # Re-read validity now the lock is held: a caller that waited
                # here may find the session it was about to replace renewed.
                if self._nest_session_valid():
                    if (
                        self._keep_warm_due()
                        and await self._async_keep_google_credentials_warm()
                    ):
                        await self._async_persist(self._client.nest_session)
                    return

                await self._async_ensure_google_token()
                # Persists the session and the refresh clock together.
                await self._async_refresh_nest_session()
        finally:
            self._notify_credentials_refreshed()

    async def async_refresh_session(self) -> bool:
        """Force-refresh the Nest session via Google credentials.

        Returns True when a fresh Nest session was obtained and persisted.
        """
        try:
            async with self._refresh_lock:
                await self._async_ensure_google_token()
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
