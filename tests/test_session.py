"""Tests for NestSessionManager."""

from __future__ import annotations

import asyncio
import datetime
import time
from http.cookies import SimpleCookie
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.nest_protect.const import BACKOFF_INTERVALS, MAX_AUTH_FAILURES
from custom_components.nest_protect.pynest.client import NestClient
from custom_components.nest_protect.pynest.exceptions import (
    NotAuthenticatedException,
    PynestException,
)
from custom_components.nest_protect.pynest.models import NestResponse
from custom_components.nest_protect.session import NestSessionManager


def _make_nest_response(*, expired: bool = False) -> NestResponse:
    """Create a NestResponse for testing."""
    if expired:
        dt = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
    else:
        dt = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=30)

    expires_str = dt.strftime("%a, %d-%b-%Y %H:%M:%S") + " GMT"

    return NestResponse(
        access_token="test-token",
        email="test@test.com",
        expires_in=expires_str,
        userid="user1",
        is_superuser=False,
        language="en",
        weave={},
        user="user.1",
        is_staff=False,
    )


def _make_first_data() -> MagicMock:
    """Create a mock FirstDataAPIResponse."""
    return MagicMock(
        updated_buckets=[],
        service_urls={"urls": {"transport_url": "https://transport.example.com"}},
    )


@pytest.mark.asyncio
async def test_restore_valid_session():
    """Test that a valid persisted session skips Google auth."""
    valid_session = _make_nest_response(expired=False)
    stored_data = {
        "nest_session": valid_session.to_dict(),
        "transport_url": "https://transport.example.com",
    }

    client = MagicMock()
    client.issue_token = "https://accounts.google.com/issue"
    client.cookies = "SID=test"
    client.refresh_token = None
    client.get_first_data = AsyncMock(return_value=_make_first_data())
    client.get_access_token_from_cookies = AsyncMock()
    client.get_access_token_from_refresh_token = AsyncMock()

    store = MagicMock()
    store.async_load = AsyncMock(return_value=stored_data)
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    result = await manager.async_setup()

    # Should return first data
    assert result is not None
    # Should NOT have called cookie auth
    client.get_access_token_from_cookies.assert_not_called()
    client.get_access_token_from_refresh_token.assert_not_called()
    # Should have called get_first_data with persisted token
    client.get_first_data.assert_called_once_with(
        valid_session.access_token, valid_session.userid
    )


@pytest.mark.asyncio
async def test_restore_expired_session_falls_through():
    """Test that an expired persisted session triggers cookie auth."""
    expired_session = _make_nest_response(expired=True)
    stored_data = {
        "nest_session": expired_session.to_dict(),
        "transport_url": "https://transport.example.com",
    }

    new_nest_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.issue_token = "https://accounts.google.com/issue"
    client.cookies = "SID=test"
    client.refresh_token = None
    client.get_access_token_from_cookies = AsyncMock(
        return_value=MagicMock(access_token="new-google-token")
    )
    client.authenticate = AsyncMock(return_value=new_nest_session)
    client.get_first_data = AsyncMock(return_value=_make_first_data())
    client.nest_session = None
    client.transport_url = None
    client.refreshed_cookies = None

    store = MagicMock()
    store.async_load = AsyncMock(return_value=stored_data)
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    result = await manager.async_setup()

    assert result is not None
    # Should have fallen through to cookie auth
    client.get_access_token_from_cookies.assert_called_once()
    client.authenticate.assert_called_once_with("new-google-token")
    # Should have persisted the new session
    store.async_save.assert_called_once()


@pytest.mark.asyncio
async def test_restore_rejected_session_falls_through():
    """Test that a 401 from Nest on persisted session triggers cookie auth."""
    valid_session = _make_nest_response(expired=False)
    stored_data = {
        "nest_session": valid_session.to_dict(),
        "transport_url": "https://transport.example.com",
    }

    new_nest_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.issue_token = "https://accounts.google.com/issue"
    client.cookies = "SID=test"
    client.refresh_token = None
    # First call with persisted token raises 401, second call succeeds
    client.get_first_data = AsyncMock(
        side_effect=[
            NotAuthenticatedException("401"),
            _make_first_data(),
        ]
    )
    client.get_access_token_from_cookies = AsyncMock(
        return_value=MagicMock(access_token="new-google-token")
    )
    client.authenticate = AsyncMock(return_value=new_nest_session)
    client.nest_session = None
    client.transport_url = None
    client.refreshed_cookies = None

    store = MagicMock()
    store.async_load = AsyncMock(return_value=stored_data)
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    result = await manager.async_setup()

    assert result is not None
    # Should have fallen through to cookie auth after 401
    client.get_access_token_from_cookies.assert_called_once()
    client.authenticate.assert_called_once_with("new-google-token")
    # Should have persisted the new session
    store.async_save.assert_called_once()


@pytest.mark.asyncio
async def test_no_persisted_session_uses_cookies():
    """Test that no stored data triggers cookie auth."""
    new_nest_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.issue_token = "https://accounts.google.com/issue"
    client.cookies = "SID=test"
    client.refresh_token = None
    client.get_access_token_from_cookies = AsyncMock(
        return_value=MagicMock(access_token="google-token")
    )
    client.authenticate = AsyncMock(return_value=new_nest_session)
    client.get_first_data = AsyncMock(return_value=_make_first_data())
    client.nest_session = None
    client.transport_url = None
    client.refreshed_cookies = None

    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    result = await manager.async_setup()

    assert result is not None
    # Should have used cookie auth
    client.get_access_token_from_cookies.assert_called_once()
    client.authenticate.assert_called_once_with("google-token")
    # Should have persisted the session
    store.async_save.assert_called_once()


@pytest.mark.asyncio
async def test_stale_persisted_refresh_time_rewarms_cookies_after_restart():
    """A restored session whose cookies are overdue refreshes them immediately.

    The refresh clock is persisted precisely so that restarting HA cannot keep
    postponing the refresh indefinitely.
    """
    valid_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.nest_session = valid_session
    client.auth = None
    client.issue_token = "https://accounts.google.com/issue"
    client.cookies = "SID=test"
    client.refresh_token = None
    client.transport_url = None
    client.get_access_token = AsyncMock()
    client.get_first_data = AsyncMock(return_value=_make_first_data())
    client.authenticate = AsyncMock()

    store = MagicMock()
    store.async_save = AsyncMock()
    store.async_load = AsyncMock(
        return_value={
            "nest_session": valid_session.to_dict(),
            "transport_url": "https://transport.example.com",
            # Last exercised two days ago — long past the refresh interval.
            "google_refreshed_at": time.time() - 2 * 24 * 60 * 60,
        }
    )

    manager = NestSessionManager(client=client, store=store)

    await manager.async_setup()
    client.get_access_token.assert_not_called()

    await manager.ensure_session()

    client.get_access_token.assert_called_once()


@pytest.mark.asyncio
async def test_recent_persisted_refresh_time_skips_google_after_restart():
    """A restored session whose cookies are still warm does not call Google."""
    valid_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.nest_session = valid_session
    client.auth = None
    client.transport_url = None
    client.get_access_token = AsyncMock()
    client.get_first_data = AsyncMock(return_value=_make_first_data())
    client.authenticate = AsyncMock()

    store = MagicMock()
    store.async_save = AsyncMock()
    store.async_load = AsyncMock(
        return_value={
            "nest_session": valid_session.to_dict(),
            "transport_url": "https://transport.example.com",
            "google_refreshed_at": time.time() - 60,
        }
    )

    manager = NestSessionManager(client=client, store=store)

    await manager.async_setup()
    await manager.ensure_session()

    client.get_access_token.assert_not_called()


@pytest.mark.asyncio
async def test_forced_refresh_ignores_interval_after_restart():
    """async_refresh_session always reaches Google, even inside the interval.

    It is the 401 recovery path, so throttling it would leave a restored
    session that Nest has rejected with no way back.
    """
    new_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.nest_session = _make_nest_response(expired=False)
    client.auth = None
    client.transport_url = None
    client.get_first_data = AsyncMock(return_value=_make_first_data())
    client.authenticate = AsyncMock(return_value=new_session)

    def set_auth(*args, **kwargs):
        client.auth = MagicMock(access_token="new-google-token")
        client.auth.is_expired = MagicMock(return_value=False)

    client.get_access_token = AsyncMock(side_effect=set_auth)

    store = MagicMock()
    store.async_save = AsyncMock()
    store.async_load = AsyncMock(
        return_value={
            "nest_session": client.nest_session.to_dict(),
            "transport_url": "https://transport.example.com",
            # Well inside the interval — ensure_session would skip Google here.
            "google_refreshed_at": time.time() - 60,
        }
    )

    manager = NestSessionManager(client=client, store=store)

    await manager.async_setup()

    assert await manager.async_refresh_session() is True
    client.get_access_token.assert_called_once()
    client.authenticate.assert_called_once_with("new-google-token")


@pytest.mark.asyncio
async def test_ensure_session_valid():
    """ensure_session is a no-op when both the Nest and Google tokens are valid."""
    valid_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.nest_session = valid_session
    client.auth = MagicMock(access_token="existing-google-token")
    client.auth.is_expired = MagicMock(return_value=False)
    client.refresh_token = "test-refresh-token"
    client.get_access_token = AsyncMock()
    client.authenticate = AsyncMock()

    store = MagicMock()
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    await manager.ensure_session()

    # Should NOT have refreshed anything
    client.get_access_token.assert_not_called()
    client.authenticate.assert_not_called()
    store.async_save.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_session_refreshes_google_token_while_nest_session_valid():
    """An expired Google token is refreshed even while the Nest session is valid.

    Regression test: Nest issues sessions lasting weeks, so gating the Google
    refresh on Nest session expiry left the auth cookies untouched for that
    whole period. Google rotates them on a far shorter cycle, so by the time
    they were next used the session had been invalidated (USER_LOGGED_OUT),
    forcing a re-authentication every day or two.
    """
    valid_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.nest_session = valid_session
    client.auth = MagicMock(access_token="stale-google-token")
    client.auth.is_expired = MagicMock(return_value=True)
    client.refresh_token = "test-refresh-token"
    client.authenticate = AsyncMock()

    def issue_fresh_token(*args, **kwargs):
        client.auth = MagicMock(access_token="fresh-google-token")
        client.auth.is_expired = MagicMock(return_value=False)

    client.get_access_token = AsyncMock(side_effect=issue_fresh_token)

    store = MagicMock()
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    await manager.ensure_session()

    # The Google token — and with it the cookies — must be refreshed.
    client.get_access_token.assert_called_once()
    # But the still-valid Nest session must not be needlessly replaced.
    client.authenticate.assert_not_called()
    # The refresh time is persisted so a restart doesn't reset the clock.
    assert store.async_save.call_count == 1
    assert store.async_save.call_args[0][0]["google_refreshed_at"] > 0


@pytest.mark.asyncio
async def test_ensure_session_expired_refreshes():
    """ensure_session refreshes when session is expired."""
    expired_session = _make_nest_response(expired=True)
    new_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.nest_session = expired_session
    client.refresh_token = "test-refresh-token"
    client.auth = MagicMock(access_token="existing-google-token")
    client.auth.is_expired = MagicMock(return_value=False)
    client.authenticate = AsyncMock(return_value=new_session)

    store = MagicMock()
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    await manager.ensure_session()

    # Should have authenticated with the existing Google token
    client.authenticate.assert_called_once_with("existing-google-token")
    # Should have persisted the new session
    store.async_save.assert_called_once()
    # Should have set the new session on the client
    assert client.nest_session == new_session


@pytest.mark.asyncio
async def test_ensure_session_none_refreshes():
    """ensure_session refreshes when no session exists."""
    new_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.nest_session = None
    client.auth = None
    client.refresh_token = "test-refresh-token"
    client.get_access_token = AsyncMock(
        return_value=MagicMock(access_token="new-google-token")
    )
    client.authenticate = AsyncMock(return_value=new_session)

    # After get_access_token is called, auth should be set
    def set_auth(*args, **kwargs):
        client.auth = MagicMock(access_token="new-google-token")
        client.auth.is_expired = MagicMock(return_value=False)

    client.get_access_token.side_effect = set_auth

    store = MagicMock()
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    await manager.ensure_session()

    # Should have fetched a new Google token
    client.get_access_token.assert_called_once()
    # Should have authenticated with the new Google token
    client.authenticate.assert_called_once_with("new-google-token")
    # Should have persisted the new session
    store.async_save.assert_called_once()
    # Should have set the new session on the client
    assert client.nest_session == new_session


@pytest.mark.asyncio
async def test_record_failure_increments_counter():
    """record_failure increments the consecutive failure counter."""
    client = MagicMock()
    store = MagicMock()

    manager = NestSessionManager(client=client, store=store)

    assert manager.consecutive_failures == 0
    manager.record_failure()
    assert manager.consecutive_failures == 1
    manager.record_failure()
    assert manager.consecutive_failures == 2


@pytest.mark.asyncio
async def test_record_success_resets_counter():
    """record_success resets the failure counter."""
    client = MagicMock()
    store = MagicMock()

    manager = NestSessionManager(client=client, store=store)

    manager.record_failure()
    manager.record_failure()
    assert manager.consecutive_failures == 2

    manager.record_success()
    assert manager.consecutive_failures == 0


@pytest.mark.asyncio
async def test_should_reauth_after_max_failures():
    """should_trigger_reauth returns True after MAX_AUTH_FAILURES."""
    client = MagicMock()
    store = MagicMock()

    manager = NestSessionManager(client=client, store=store)

    # Should be False initially
    assert manager.should_trigger_reauth is False

    # Record failures up to threshold
    for _ in range(MAX_AUTH_FAILURES - 1):
        manager.record_failure()
    assert manager.should_trigger_reauth is False

    # One more failure should trigger reauth
    manager.record_failure()
    assert manager.should_trigger_reauth is True


@pytest.mark.asyncio
async def test_backoff_interval_increases():
    """backoff_interval returns increasing values."""
    client = MagicMock()
    store = MagicMock()

    manager = NestSessionManager(client=client, store=store)

    # At 0 failures, should return the first interval
    assert manager.backoff_interval == BACKOFF_INTERVALS[0]

    # Each failure should increase the backoff
    manager.record_failure()
    assert manager.backoff_interval == BACKOFF_INTERVALS[0]

    manager.record_failure()
    assert manager.backoff_interval == BACKOFF_INTERVALS[1]

    manager.record_failure()
    assert manager.backoff_interval == BACKOFF_INTERVALS[2]

    # Should cap at the last interval
    for _ in range(10):
        manager.record_failure()
    assert manager.backoff_interval == BACKOFF_INTERVALS[-1]


@pytest.mark.asyncio
async def test_concurrent_ensure_session_coalesces_google_refresh():
    """Concurrent callers must not each rotate the cookies.

    The subscriber, entities and the lock observer share one manager. Two
    refreshes in flight would both rotate, and only the last response would be
    kept — discarding the cookie Google now expects.
    """
    expired_auth = MagicMock(access_token="stale")
    expired_auth.is_expired = MagicMock(return_value=True)

    client = MagicMock()
    client.nest_session = _make_nest_response(expired=False)
    client.auth = expired_auth
    client.transport_url = None
    client.authenticate = AsyncMock()

    async def refresh(*args, **kwargs):
        await asyncio.sleep(0)
        fresh = MagicMock(access_token="fresh")
        fresh.is_expired = MagicMock(return_value=False)
        client.auth = fresh

    client.get_access_token = AsyncMock(side_effect=refresh)

    store = MagicMock()
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    await asyncio.gather(manager.ensure_session(), manager.ensure_session())

    client.get_access_token.assert_called_once()


@pytest.mark.asyncio
async def test_missing_credentials_do_not_advance_refresh_clock():
    """A refresh that produced no token must not count as one."""
    client = MagicMock()
    client.nest_session = _make_nest_response(expired=False)
    client.auth = None
    client.transport_url = None
    client.refreshed_cookies = None
    client.get_access_token = AsyncMock(return_value=None)
    client.authenticate = AsyncMock()

    store = MagicMock()
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    assert await manager.ensure_google_credentials(force=True) is False
    store.async_save.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_store_persists_assumed_refresh_clock():
    """A store predating the clock writes the assumed value back.

    Otherwise every restart would assume "fresh" again, letting a frequently
    restarted instance postpone the refresh indefinitely.
    """
    valid_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.nest_session = None
    client.auth = None
    client.transport_url = None
    client.get_access_token = AsyncMock()
    client.get_first_data = AsyncMock(return_value=_make_first_data())

    store = MagicMock()
    store.async_save = AsyncMock()
    store.async_load = AsyncMock(
        return_value={
            "nest_session": valid_session.to_dict(),
            "transport_url": "https://transport.example.com",
        }
    )

    manager = NestSessionManager(client=client, store=store)

    await manager.async_setup()

    store.async_save.assert_called_once()
    assert store.async_save.call_args[0][0]["google_refreshed_at"] > 0


@pytest.mark.asyncio
async def test_future_refresh_time_does_not_suppress_refresh():
    """A corrupt or skewed future timestamp must not disable refreshing."""
    valid_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.nest_session = None
    client.auth = None
    client.transport_url = None
    client.get_access_token = AsyncMock()
    client.get_first_data = AsyncMock(return_value=_make_first_data())

    store = MagicMock()
    store.async_save = AsyncMock()
    store.async_load = AsyncMock(
        return_value={
            "nest_session": valid_session.to_dict(),
            "transport_url": "https://transport.example.com",
            "google_refreshed_at": time.time() + 365 * 24 * 60 * 60,
        }
    )

    manager = NestSessionManager(client=client, store=store)

    await manager.async_setup()

    # Clamped to now and written back, rather than trusted.
    store.async_save.assert_called_once()
    assert store.async_save.call_args[0][0]["google_refreshed_at"] <= time.time()


@pytest.mark.asyncio
async def test_refresh_notifies_caller_to_persist_rotated_cookies():
    """Every refresh path reports back so rotated cookies can be persisted.

    Entity updates refresh through the manager directly and have no
    persistence step of their own.
    """
    expired_auth = MagicMock(access_token="stale")
    expired_auth.is_expired = MagicMock(return_value=True)

    client = MagicMock()
    client.nest_session = _make_nest_response(expired=False)
    client.auth = expired_auth
    client.transport_url = None
    client.authenticate = AsyncMock()

    def refresh(*args, **kwargs):
        fresh = MagicMock(access_token="fresh")
        fresh.is_expired = MagicMock(return_value=False)
        client.auth = fresh

    client.get_access_token = AsyncMock(side_effect=refresh)

    store = MagicMock()
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)
    manager.on_credentials_refreshed = MagicMock()

    await manager.ensure_session()

    manager.on_credentials_refreshed.assert_called_once()


@pytest.mark.asyncio
async def test_concurrent_expired_session_coalesces_nest_authentication():
    """The whole refresh is single-flight, not just the Google half.

    Two callers finding an expired Nest session would otherwise authenticate
    twice and write twice, and a reordered response could leave the client
    holding the session Nest had already superseded.
    """
    fresh_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.nest_session = _make_nest_response(expired=True)
    client.auth = MagicMock(access_token="google-token")
    client.auth.is_expired = MagicMock(return_value=False)
    client.transport_url = None

    async def authenticate(_token):
        await asyncio.sleep(0)
        return fresh_session

    client.authenticate = AsyncMock(side_effect=authenticate)

    store = MagicMock()
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    await asyncio.gather(manager.ensure_session(), manager.ensure_session())

    assert client.authenticate.call_count == 1
    assert store.async_save.call_count == 1


@pytest.mark.asyncio
async def test_startup_rotation_is_notified_before_nest_authentication():
    """Cookies rotated at startup survive a later failure talking to Nest.

    Google has already moved on by the time authenticate() runs, so a retry
    starting from the superseded cookies would be rejected.
    """
    client = MagicMock()
    client.nest_session = None
    client.issue_token = "https://accounts.google.com/issue"
    client.cookies = "SID=old"
    client.refresh_token = None
    client.transport_url = None

    def rotate_cookies(*args, **kwargs):
        client.refreshed_cookies = "SID=new"
        client.cookies = "SID=new"
        return MagicMock(access_token="google-token")

    client.get_access_token_from_cookies = AsyncMock(side_effect=rotate_cookies)
    client.authenticate = AsyncMock(side_effect=PynestException("Nest unavailable"))

    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)
    manager.on_credentials_refreshed = MagicMock()

    with pytest.raises(PynestException):
        await manager.async_setup()

    manager.on_credentials_refreshed.assert_called_once()


@pytest.mark.asyncio
async def test_unchanged_expired_token_is_not_reported_as_refreshed():
    """A token request that changed nothing must not advance the clock."""
    expired_auth = MagicMock(access_token="expired")
    expired_auth.is_expired = MagicMock(return_value=True)

    client = MagicMock()
    client.nest_session = None
    client.auth = expired_auth
    client.refreshed_cookies = None
    client.get_access_token = AsyncMock(return_value=expired_auth)

    store = MagicMock()
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)
    manager.on_credentials_refreshed = MagicMock()

    assert await manager.ensure_google_credentials(force=True) is False
    manager.on_credentials_refreshed.assert_not_called()
    store.async_save.assert_not_called()


def _rotation_then_nest_failure_manager():
    """Build a manager whose Google refresh succeeds but Nest then fails."""
    stale_auth = MagicMock(access_token="stale")
    stale_auth.is_expired = MagicMock(return_value=True)

    client = MagicMock()
    client.auth = stale_auth
    client.nest_session = _make_nest_response(expired=True)
    client.transport_url = None

    async def rotate_credentials():
        fresh_auth = MagicMock(access_token="fresh")
        fresh_auth.is_expired = MagicMock(return_value=False)
        client.auth = fresh_auth
        client.refreshed_cookies = "SID=new"

    client.get_access_token = AsyncMock(side_effect=rotate_credentials)
    client.authenticate = AsyncMock(side_effect=PynestException("Nest unavailable"))

    store = MagicMock()
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)
    manager.on_credentials_refreshed = MagicMock()
    return manager, client


@pytest.mark.asyncio
async def test_ensure_session_notifies_rotation_when_nest_auth_fails():
    """Cookies rotated before a Nest failure still reach the config entry.

    Otherwise the in-memory client holds the new cookies while the entry keeps
    the superseded set, and a restart reproduces the very USER_LOGGED_OUT this
    is meant to prevent.
    """
    manager, _client = _rotation_then_nest_failure_manager()

    with pytest.raises(PynestException):
        await manager.ensure_session()

    manager.on_credentials_refreshed.assert_called_once()


@pytest.mark.asyncio
async def test_forced_refresh_notifies_rotation_when_nest_auth_fails():
    """Same guarantee on the 401 recovery path."""
    manager, _client = _rotation_then_nest_failure_manager()

    with pytest.raises(PynestException):
        await manager.async_refresh_session()

    manager.on_credentials_refreshed.assert_called_once()


@pytest.mark.asyncio
async def test_refresh_lock_is_released_after_nest_auth_failure():
    """A failed refresh must not strand the lock and wedge every later one."""
    manager, client = _rotation_then_nest_failure_manager()

    with pytest.raises(PynestException):
        await manager.async_refresh_session()

    client.authenticate = AsyncMock(return_value=_make_nest_response(expired=False))

    assert await asyncio.wait_for(manager.async_refresh_session(), timeout=1) is True


@pytest.mark.asyncio
async def test_clock_rollback_does_not_suppress_due_refresh():
    """A backwards system clock must not park the refresh indefinitely."""
    client = MagicMock()
    client.auth = None
    client.nest_session = _make_nest_response(expired=False)
    client.transport_url = None

    async def refresh_credentials():
        fresh_auth = MagicMock(access_token="fresh")
        fresh_auth.is_expired = MagicMock(return_value=False)
        client.auth = fresh_auth

    client.get_access_token = AsyncMock(side_effect=refresh_credentials)
    client.authenticate = AsyncMock()

    store = MagicMock()
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)
    # Recorded before the clock jumped backwards.
    manager._google_refreshed_at = time.time() + 10_000

    await manager.ensure_session()

    client.get_access_token.assert_called_once()


class _RotatingThenFailingResponse:
    """Google response that rotates cookies before the body read fails."""

    def __init__(self) -> None:
        self.cookies = SimpleCookie()
        self.cookies["SID"] = "new"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        raise PynestException("connection dropped reading the body")


class _RotatingThenFailingSession:
    """Minimal ClientSession stand-in returning the response above."""

    def get(self, *args, **kwargs):
        return _RotatingThenFailingResponse()


@pytest.mark.asyncio
async def test_rotation_is_notified_when_google_body_read_fails():
    """Set-Cookie is already authoritative when the body read then fails.

    The client applies the rotated cookies before awaiting the response body,
    so a failure there would otherwise leave the config entry holding a set
    Google has already superseded.
    """
    client = NestClient(session=_RotatingThenFailingSession())
    client.issue_token = "https://accounts.google.com/issue"
    client.cookies = "SID=old"
    client.nest_session = _make_nest_response(expired=False)

    manager = NestSessionManager(client=client, store=MagicMock())
    manager.on_credentials_refreshed = MagicMock()

    with pytest.raises(PynestException):
        await manager.ensure_session()

    assert client.refreshed_cookies == "SID=new"
    manager.on_credentials_refreshed.assert_called_once_with()
    assert manager._refresh_lock.locked() is False
