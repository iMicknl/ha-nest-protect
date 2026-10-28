"""Tests for NestSessionManager."""

from __future__ import annotations

import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.nest_protect.const import BACKOFF_INTERVALS, MAX_AUTH_FAILURES
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
async def test_ensure_session_valid():
    """ensure_session is a no-op when session is still valid."""
    valid_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.nest_session = valid_session
    client.auth = None
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
async def test_record_auth_failure_increments_counter():
    """record_auth_failure increments the consecutive auth failure counter."""
    client = MagicMock()
    store = MagicMock()

    manager = NestSessionManager(client=client, store=store)

    assert manager.consecutive_auth_failures == 0
    manager.record_auth_failure()
    assert manager.consecutive_auth_failures == 1
    manager.record_auth_failure()
    assert manager.consecutive_auth_failures == 2


@pytest.mark.asyncio
async def test_record_success_resets_counter():
    """record_success resets the auth failure counter."""
    client = MagicMock()
    store = MagicMock()

    manager = NestSessionManager(client=client, store=store)

    manager.record_auth_failure()
    manager.record_auth_failure()
    assert manager.consecutive_auth_failures == 2

    manager.record_success()
    assert manager.consecutive_auth_failures == 0


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
        manager.record_auth_failure()
    assert manager.should_trigger_reauth is False

    # One more failure should trigger reauth
    manager.record_auth_failure()
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
    manager.record_auth_failure()
    assert manager.backoff_interval == BACKOFF_INTERVALS[0]

    manager.record_auth_failure()
    assert manager.backoff_interval == BACKOFF_INTERVALS[1]

    manager.record_auth_failure()
    assert manager.backoff_interval == BACKOFF_INTERVALS[2]

    # Should cap at the last interval
    for _ in range(10):
        manager.record_auth_failure()
    assert manager.backoff_interval == BACKOFF_INTERVALS[-1]


def _make_refresh_client(
    nest_session: NestResponse | None, *new_sessions: NestResponse
) -> tuple[MagicMock, asyncio.Event]:
    """Create a client whose Google token fetch blocks until the event is set."""
    client = MagicMock()
    client.nest_session = nest_session
    client.auth = None
    client.transport_url = "https://transport.example.com"
    release = asyncio.Event()

    async def get_access_token():
        await release.wait()
        client.auth = MagicMock(access_token="new-google-token")
        client.auth.is_expired = MagicMock(return_value=False)

    client.get_access_token = AsyncMock(side_effect=get_access_token)
    client.authenticate = AsyncMock(side_effect=list(new_sessions))
    return client, release


async def _run_concurrently(release: asyncio.Event, *coros) -> list:
    """Start all coroutines, let them reach the lock, then unblock the refresh."""
    tasks = [asyncio.create_task(coro) for coro in coros]
    await asyncio.sleep(0)
    release.set()
    return await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_concurrent_ensure_session_refreshes_once():
    """Concurrent ensure_session calls on an expired session share one refresh."""
    new_session = _make_nest_response(expired=False)
    client, release = _make_refresh_client(
        _make_nest_response(expired=True), new_session
    )
    store = MagicMock()
    store.async_save = AsyncMock()
    manager = NestSessionManager(client=client, store=store)

    await _run_concurrently(release, *(manager.ensure_session() for _ in range(3)))

    client.get_access_token.assert_called_once()
    client.authenticate.assert_called_once_with("new-google-token")
    store.async_save.assert_called_once()
    assert client.nest_session is new_session


@pytest.mark.asyncio
async def test_concurrent_forced_refresh_refreshes_once():
    """Concurrent forced refreshes of the same rejected session share one refresh."""
    new_session = _make_nest_response(expired=False)
    client, release = _make_refresh_client(
        _make_nest_response(expired=False), new_session
    )
    store = MagicMock()
    store.async_save = AsyncMock()
    manager = NestSessionManager(client=client, store=store)

    results = await _run_concurrently(
        release,
        manager.async_refresh_session(),
        manager.async_refresh_session(),
        manager.ensure_session(),
    )

    assert results[:2] == [True, True]
    client.get_access_token.assert_called_once()
    client.authenticate.assert_called_once_with("new-google-token")
    store.async_save.assert_called_once()
    assert client.nest_session is new_session


@pytest.mark.asyncio
async def test_forced_refresh_ignores_unexpired_session():
    """A forced refresh after a 401 still refreshes an unexpired session."""
    first_session = _make_nest_response(expired=False)
    second_session = _make_nest_response(expired=False)
    client, release = _make_refresh_client(
        _make_nest_response(expired=False), first_session, second_session
    )
    release.set()
    store = MagicMock()
    store.async_save = AsyncMock()
    manager = NestSessionManager(client=client, store=store)

    assert await manager.async_refresh_session() is True
    assert await manager.async_refresh_session() is True

    assert client.authenticate.call_count == 2
    assert client.nest_session is second_session


@pytest.mark.asyncio
async def test_waiter_refreshes_when_inflight_refresh_fails():
    """A waiter performs its own refresh if the one it waited for failed."""
    new_session = _make_nest_response(expired=False)
    client, release = _make_refresh_client(
        _make_nest_response(expired=True),
        NotAuthenticatedException("401"),
        new_session,
    )
    store = MagicMock()
    store.async_save = AsyncMock()
    manager = NestSessionManager(client=client, store=store)

    tasks = [asyncio.create_task(manager.ensure_session()) for _ in range(2)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert isinstance(results[0], NotAuthenticatedException)
    assert results[1] is None
    assert client.authenticate.call_count == 2
    assert client.nest_session is new_session


def _make_persisting_client(
    nest_session: NestResponse, *new_sessions: NestResponse
) -> MagicMock:
    """Create a client whose authenticate() replaces nest_session, like NestClient."""
    client = MagicMock()
    client.nest_session = nest_session
    client.auth = MagicMock(access_token="google-token")
    client.auth.is_expired = MagicMock(return_value=False)
    client.transport_url = "https://transport.example.com"
    sessions = iter(new_sessions)

    async def authenticate(access_token):
        # Yield like the real request, so a concurrent caller sees the old session.
        await asyncio.sleep(0)
        client.nest_session = next(sessions)
        return client.nest_session

    client.authenticate = AsyncMock(side_effect=authenticate)
    return client


def _make_blocking_store(
    *outcomes: Exception | None,
) -> tuple[MagicMock, asyncio.Event, asyncio.Event]:
    """Create a store whose first async_save blocks until released."""
    store = MagicMock()
    saving = asyncio.Event()
    release = asyncio.Event()
    results = iter(outcomes)

    async def async_save(data):
        if not saving.is_set():
            saving.set()
            await release.wait()
        if (error := next(results)) is not None:
            raise error

    store.async_save = AsyncMock(side_effect=async_save)
    return store, saving, release


@pytest.mark.asyncio
async def test_forced_refresh_during_persist_shares_refresh():
    """A forced refresh that starts while the session is persisted reuses it."""
    new_session = _make_nest_response(expired=False)
    client = _make_persisting_client(
        _make_nest_response(expired=False), new_session, _make_nest_response()
    )
    store, saving, release = _make_blocking_store(None, None)
    manager = NestSessionManager(client=client, store=store)

    first = asyncio.create_task(manager.async_refresh_session())
    await saving.wait()
    second = asyncio.create_task(manager.async_refresh_session())
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(first, second) == [True, True]
    client.authenticate.assert_called_once()
    assert client.nest_session is new_session


@pytest.mark.asyncio
async def test_waiter_refreshes_when_inflight_persist_fails():
    """A waiter must not report success when the refresh it waited for failed."""
    second_session = _make_nest_response(expired=False)
    client = _make_persisting_client(
        _make_nest_response(expired=False), _make_nest_response(), second_session
    )
    store, saving, release = _make_blocking_store(OSError("disk full"), None)
    manager = NestSessionManager(client=client, store=store)

    first = asyncio.create_task(manager.async_refresh_session())
    second = asyncio.create_task(manager.async_refresh_session())
    await saving.wait()
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert isinstance(results[0], OSError)
    assert results[1] is True
    assert client.authenticate.call_count == 2
    assert store.async_save.call_count == 2
    assert client.nest_session is second_session


@pytest.mark.asyncio
async def test_record_error_does_not_count_toward_reauth():
    """Non-auth errors must never push the session toward reauth."""
    manager = NestSessionManager(client=MagicMock(), store=MagicMock())

    for _ in range(MAX_AUTH_FAILURES + 1):
        manager.record_error()

    assert manager.consecutive_errors == MAX_AUTH_FAILURES + 1
    assert manager.consecutive_auth_failures == 0
    assert manager.should_trigger_reauth is False
    assert manager.backoff_interval == BACKOFF_INTERVALS[0]


@pytest.mark.asyncio
async def test_error_backoff_interval_increases():
    """error_backoff_interval escalates with consecutive non-auth errors."""
    manager = NestSessionManager(client=MagicMock(), store=MagicMock())

    assert manager.error_backoff_interval == BACKOFF_INTERVALS[0]

    manager.record_error()
    assert manager.error_backoff_interval == BACKOFF_INTERVALS[0]

    manager.record_error()
    assert manager.error_backoff_interval == BACKOFF_INTERVALS[1]

    for _ in range(10):
        manager.record_error()
    assert manager.error_backoff_interval == BACKOFF_INTERVALS[-1]

    manager.record_auth_failure()
    assert manager.error_backoff_interval == BACKOFF_INTERVALS[-1]
    assert manager.backoff_interval == BACKOFF_INTERVALS[0]


@pytest.mark.asyncio
async def test_record_auth_success_keeps_error_counter():
    """An accepted session clears auth failures but not unrelated errors."""
    manager = NestSessionManager(client=MagicMock(), store=MagicMock())
    manager.record_auth_failure()
    manager.record_error()

    manager.record_auth_success()

    assert manager.consecutive_auth_failures == 0
    assert manager.consecutive_errors == 1


@pytest.mark.asyncio
async def test_record_success_resets_both_counters():
    """A fully successful cycle clears auth failures and errors."""
    manager = NestSessionManager(client=MagicMock(), store=MagicMock())
    manager.record_auth_failure()
    manager.record_error()

    manager.record_success()

    assert manager.consecutive_auth_failures == 0
    assert manager.consecutive_errors == 0


ROTATED_COOKIES = "SID=rotated; HSID=test"


def _make_rotating_client() -> MagicMock:
    """Create a client whose cookie exchange rotates cookies but Nest auth fails."""
    client = MagicMock()
    client.issue_token = "https://accounts.google.com/issue"
    client.cookies = "SID=test; HSID=test"
    client.refresh_token = None
    client.nest_session = None
    client.auth = None
    client.refreshed_cookies = None

    def rotate(*args):
        client.refreshed_cookies = ROTATED_COOKIES
        client.auth = MagicMock(access_token="google-token")
        client.auth.is_expired = MagicMock(return_value=False)
        return client.auth

    client.get_access_token_from_cookies = AsyncMock(side_effect=rotate)
    client.get_access_token = AsyncMock(side_effect=rotate)
    client.authenticate = AsyncMock(side_effect=PynestException("session failed"))
    return client


@pytest.mark.asyncio
async def test_setup_reports_rotated_cookies_when_nest_auth_fails():
    """Rotated cookies are handed over even if the JWT/session exchange fails."""
    client = _make_rotating_client()
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    on_cookies_refreshed = MagicMock()

    manager = NestSessionManager(
        client=client, store=store, on_cookies_refreshed=on_cookies_refreshed
    )

    with pytest.raises(PynestException):
        await manager.async_setup()

    on_cookies_refreshed.assert_called_once_with(ROTATED_COOKIES)


@pytest.mark.asyncio
async def test_refresh_reports_rotated_cookies_when_nest_auth_fails():
    """Background refresh hands over rotated cookies before Nest auth runs."""
    client = _make_rotating_client()
    on_cookies_refreshed = MagicMock()

    manager = NestSessionManager(
        client=client, store=MagicMock(), on_cookies_refreshed=on_cookies_refreshed
    )

    with pytest.raises(PynestException):
        await manager.async_refresh_session()

    on_cookies_refreshed.assert_called_once_with(ROTATED_COOKIES)


@pytest.mark.asyncio
async def test_reports_rotated_cookies_when_token_exchange_fails():
    """Cookies rotated on a failed token exchange are still handed over."""
    client = _make_rotating_client()

    def rotate_then_fail(*args):
        client.refreshed_cookies = ROTATED_COOKIES
        raise PynestException("token exchange failed")

    client.get_access_token_from_cookies.side_effect = rotate_then_fail
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    on_cookies_refreshed = MagicMock()

    manager = NestSessionManager(
        client=client, store=store, on_cookies_refreshed=on_cookies_refreshed
    )

    with pytest.raises(PynestException):
        await manager.async_setup()

    on_cookies_refreshed.assert_called_once_with(ROTATED_COOKIES)


@pytest.mark.asyncio
async def test_no_report_without_rotated_cookies():
    """Nothing is handed over when Google did not rotate cookies."""
    client = _make_rotating_client()
    client.get_access_token_from_cookies.side_effect = None
    client.get_access_token_from_cookies.return_value = MagicMock(
        access_token="google-token"
    )
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    on_cookies_refreshed = MagicMock()

    manager = NestSessionManager(
        client=client, store=store, on_cookies_refreshed=on_cookies_refreshed
    )

    with pytest.raises(PynestException):
        await manager.async_setup()

    on_cookies_refreshed.assert_not_called()


def _make_command_manager() -> NestSessionManager:
    """Create a manager whose session is valid and whose refresh succeeds."""
    manager = NestSessionManager(client=MagicMock(), store=MagicMock())
    manager.ensure_session = AsyncMock()
    manager.async_refresh_session = AsyncMock(return_value=True)
    return manager


@pytest.mark.asyncio
async def test_command_runs_once_with_a_valid_session():
    """A successful command is neither refreshed nor retried."""
    manager = _make_command_manager()
    command = AsyncMock(return_value="ok")

    result = await manager.async_run_command(command, NotAuthenticatedException)

    assert result == "ok"
    manager.ensure_session.assert_awaited_once()
    manager.async_refresh_session.assert_not_called()
    command.assert_awaited_once()


@pytest.mark.asyncio
async def test_command_refreshes_and_retries_once_on_auth_error():
    """An auth rejection force-refreshes the session and retries the command."""
    manager = _make_command_manager()
    command = AsyncMock(side_effect=[NotAuthenticatedException("401"), "ok"])

    result = await manager.async_run_command(command, NotAuthenticatedException)

    assert result == "ok"
    manager.async_refresh_session.assert_awaited_once()
    assert command.await_count == 2


@pytest.mark.asyncio
async def test_command_retries_only_once():
    """A command still rejected after a refresh fails instead of looping."""
    manager = _make_command_manager()
    command = AsyncMock(side_effect=NotAuthenticatedException("401"))

    with pytest.raises(NotAuthenticatedException):
        await manager.async_run_command(command, NotAuthenticatedException)

    manager.async_refresh_session.assert_awaited_once()
    assert command.await_count == 2


@pytest.mark.asyncio
async def test_command_is_not_retried_on_other_errors():
    """Only auth errors justify a session refresh."""
    manager = _make_command_manager()
    command = AsyncMock(side_effect=PynestException("500"))

    with pytest.raises(PynestException):
        await manager.async_run_command(command, NotAuthenticatedException)

    manager.async_refresh_session.assert_not_called()
    command.assert_awaited_once()


@pytest.mark.asyncio
async def test_command_is_not_retried_without_a_new_session():
    """Retrying with the session that was just rejected cannot succeed."""
    manager = _make_command_manager()
    manager.async_refresh_session.return_value = False
    command = AsyncMock(side_effect=NotAuthenticatedException("401"))

    with pytest.raises(NotAuthenticatedException):
        await manager.async_run_command(command, NotAuthenticatedException)

    command.assert_awaited_once()
