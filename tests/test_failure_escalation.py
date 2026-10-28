"""Failure paths must escalate their backoff or end in re-authentication."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect import (
    DOMAIN,
    HomeAssistantNestProtectData,
    _async_observe_locks_loop,
    _async_subscribe_for_data,
    _cookie_persister,
)
from custom_components.nest_protect.const import BACKOFF_INTERVALS, MAX_AUTH_FAILURES
from custom_components.nest_protect.lock import NestLockEntity
from custom_components.nest_protect.pynest.client import NestClient
from custom_components.nest_protect.pynest.exceptions import (
    BadCredentialsException,
    NestLockAuthException,
    NestServiceException,
    PynestException,
)
from custom_components.nest_protect.session import NestSessionManager

from .conftest import COOKIES, ISSUE_TOKEN


def _entry_data(hass, entry):
    client = MagicMock()
    # An expired Nest session sends every subscribe through a Google refresh.
    client.nest_session = MagicMock(is_expired=lambda buffer_seconds=0: True)
    client.refreshed_cookies = None
    client.auth = None
    store = MagicMock()
    store.async_save = AsyncMock()
    sm = NestSessionManager(
        client,
        store,
        on_cookies_refreshed=_cookie_persister(hass, entry),
    )
    entry.runtime_data = HomeAssistantNestProtectData(
        devices={},
        areas={},
        client=client,
        session_manager=sm,
        grpc_lock_client=MagicMock(),
        stream_health=MagicMock(),
    )
    return client, sm


def _subscribe_data():
    data = MagicMock()
    data.updated_buckets = []
    return data


@pytest.mark.parametrize(
    "google_error",
    [
        PynestException("200 unexpected Google access token response"),
        NestServiceException("429 error while retrieving Google access token"),
    ],
    ids=["unexpected_response", "throttled"],
)
async def test_google_refresh_failures_escalate_backoff(hass, google_error):
    """A Google refresh that keeps failing is retried less and less often."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    client, sm = _entry_data(hass, entry)
    client.get_access_token = AsyncMock(side_effect=google_error)

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch(
            "custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock
        ) as sleep,
        patch.object(entry, "async_start_reauth") as reauth,
    ):
        for _ in range(8):
            await _async_subscribe_for_data(hass, entry, _subscribe_data())

    delays = [c.args[0] for c in sleep.call_args_list if c.args[0]]
    assert delays == sorted(delays)
    assert delays[-1] == BACKOFF_INTERVALS[-1]
    assert sm.consecutive_errors == 8
    reauth.assert_not_called()


async def test_lock_stream_rejected_after_a_batch_escalates_to_reauth(hass):
    """A stream that sends its initial state and is then rejected can't loop."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    _, sm = _entry_data(hass, entry)

    async def observe_locks():
        yield {}
        raise NestLockAuthException("UNAUTHENTICATED in stream status")

    entry.runtime_data.grpc_lock_client.observe_locks = observe_locks

    with (
        patch.object(sm, "async_refresh_session", return_value=True) as refresh,
        patch(
            "custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock
        ) as sleep,
        patch.object(entry, "async_start_reauth") as reauth,
    ):
        await _async_observe_locks_loop(hass, entry)

    reauth.assert_called_once()
    assert refresh.await_count == MAX_AUTH_FAILURES - 1
    assert [c.args[0] for c in sleep.call_args_list] == list(
        BACKOFF_INTERVALS[: MAX_AUTH_FAILURES - 1]
    )


async def test_lock_stream_failures_far_apart_do_not_add_up(hass):
    """An auth failure long after the last one, with data in between, starts over."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    _, sm = _entry_data(hass, entry)
    streams = 0

    async def observe_locks():
        nonlocal streams
        streams += 1
        if streams > MAX_AUTH_FAILURES + 1:
            raise asyncio.CancelledError
        yield {}
        raise NestLockAuthException("UNAUTHENTICATED in stream status")

    entry.runtime_data.grpc_lock_client.observe_locks = observe_locks
    clock = iter(range(0, 100_000, 1_000))

    with (
        patch.object(sm, "async_refresh_session", return_value=True),
        patch("custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "custom_components.nest_protect.time.monotonic",
            side_effect=lambda: float(next(clock)),
        ),
        patch.object(entry, "async_start_reauth") as reauth,
        pytest.raises(asyncio.CancelledError),
    ):
        await _async_observe_locks_loop(hass, entry)

    reauth.assert_not_called()


async def test_runtime_refresh_prefers_cookies_over_legacy_refresh_token():
    """An entry re-authenticated with cookies must not refresh with its old token."""
    client = NestClient(session=MagicMock())
    client.refresh_token = "legacy-revoked"
    client.issue_token = ISSUE_TOKEN
    client.cookies = COOKIES
    store = MagicMock()
    store.async_save = AsyncMock()
    sm = NestSessionManager(client, store)

    async def cookie_auth(issue_token, cookies):
        client.auth = MagicMock(access_token="google-token", is_expired=lambda: False)
        return client.auth

    with (
        patch.object(client, "get_access_token_from_refresh_token") as legacy,
        patch.object(
            client, "get_access_token_from_cookies", side_effect=cookie_auth
        ) as cookie,
        patch.object(client, "authenticate", return_value=MagicMock(to_dict=dict)),
    ):
        assert await sm.async_refresh_session() is True

    cookie.assert_awaited_once()
    legacy.assert_not_called()


async def test_lock_command_with_rejected_credentials_starts_reauth(hass):
    """A refresh Google rejects during a command asks the user to re-authenticate."""
    client = MagicMock()
    client.nest_session = MagicMock(is_expired=lambda buffer_seconds=0: False)
    client.refreshed_cookies = None
    client.auth = None
    client.get_access_token = AsyncMock(side_effect=BadCredentialsException("x"))
    sm = NestSessionManager(client, MagicMock())
    grpc = MagicMock()
    grpc.send_lock_command = AsyncMock(side_effect=NestLockAuthException("401"))
    entity = NestLockEntity(grpc, sm, MagicMock(resource_id="r", serial_number="s"))
    entity.hass = hass
    entity.platform = MagicMock()

    with pytest.raises(HomeAssistantError):
        await entity.async_lock()

    entity.platform.config_entry.async_start_reauth.assert_called_once_with(hass)
