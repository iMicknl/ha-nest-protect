"""Tests for NestSessionManager."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.nest_protect.pynest.exceptions import (
    NotAuthenticatedException,
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
    client.get_first_data = AsyncMock(return_value=_make_first_data())
    client.get_access_token_from_cookies = AsyncMock()
    client.get_access_token_from_refresh_token = AsyncMock()

    store = MagicMock()
    store.async_load = AsyncMock(return_value=stored_data)
    store.async_save = AsyncMock()

    manager = NestSessionManager(
        client=client,
        store=store,
        issue_token="https://accounts.google.com/issue",
        cookies="SID=test",
    )

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

    manager = NestSessionManager(
        client=client,
        store=store,
        issue_token="https://accounts.google.com/issue",
        cookies="SID=test",
    )

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

    manager = NestSessionManager(
        client=client,
        store=store,
        issue_token="https://accounts.google.com/issue",
        cookies="SID=test",
    )

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

    manager = NestSessionManager(
        client=client,
        store=store,
        issue_token="https://accounts.google.com/issue",
        cookies="SID=test",
    )

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
    client.get_access_token = AsyncMock()
    client.authenticate = AsyncMock()

    store = MagicMock()
    store.async_save = AsyncMock()

    manager = NestSessionManager(
        client=client,
        store=store,
        refresh_token="test-refresh-token",
    )

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
    client.auth = MagicMock(access_token="existing-google-token")
    client.auth.is_expired = MagicMock(return_value=False)
    client.authenticate = AsyncMock(return_value=new_session)

    store = MagicMock()
    store.async_save = AsyncMock()

    manager = NestSessionManager(
        client=client,
        store=store,
        refresh_token="test-refresh-token",
    )

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

    manager = NestSessionManager(
        client=client,
        store=store,
        refresh_token="test-refresh-token",
    )

    await manager.ensure_session()

    # Should have fetched a new Google token
    client.get_access_token.assert_called_once()
    # Should have authenticated with the new Google token
    client.authenticate.assert_called_once_with("new-google-token")
    # Should have persisted the new session
    store.async_save.assert_called_once()
    # Should have set the new session on the client
    assert client.nest_session == new_session
