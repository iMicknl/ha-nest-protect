"""Test init."""

import asyncio
import contextlib
import datetime
import logging
from contextlib import nullcontext
from functools import partial
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect import (
    DOMAIN,
    HomeAssistantNestProtectData,
    _async_observe_locks_loop,
    _async_subscribe_for_data,
    _persist_refreshed_cookies,
    _register_subscribe_task,
)
from custom_components.nest_protect.const import (
    BACKOFF_INTERVALS,
    CONF_COOKIES,
    MAX_AUTH_FAILURES,
    RECONNECT_INTERVALS,
    SUBSCRIBE_FAST_RESPONSE_WARNING,
    SUBSCRIBE_HEALTHY_SECONDS,
)
from custom_components.nest_protect.pynest.client import NestClient
from custom_components.nest_protect.pynest.exceptions import (
    BadCredentialsException,
    BadGatewayException,
    EmptyResponseException,
    NestLockAuthException,
    NestServiceException,
    NotAuthenticatedException,
    PynestException,
)
from custom_components.nest_protect.session import NestSessionManager

from .conftest import COOKIES, ISSUE_TOKEN, ComponentSetup


@pytest.mark.skip(
    reason="Needs to be fixed. _async_subscribe_for_data should be cancelled when the component is unloaded."
)
async def test_init_with_refresh_token(
    hass,
    component_setup_with_refresh_token: ComponentSetup,
    config_entry_with_refresh_token: MockConfigEntry,
):
    """Test initialization."""
    with (
        patch(
            "custom_components.nest_protect.NestClient.get_access_token_from_refresh_token"
        ),
        patch("custom_components.nest_protect.NestClient.authenticate"),
        patch("custom_components.nest_protect.NestClient.get_first_data"),
        patch("custom_components.nest_protect.Store.async_load", return_value=None),
        patch("custom_components.nest_protect.Store.async_save"),
    ):
        await component_setup_with_refresh_token()

    assert config_entry_with_refresh_token.state is ConfigEntryState.LOADED


async def test_access_token_failure_with_refresh_token(
    hass,
    component_setup_with_refresh_token: ComponentSetup,
    config_entry_with_refresh_token: MockConfigEntry,
):
    """Test failure when getting an access token."""
    with (
        patch("custom_components.nest_protect.Store.async_load", return_value=None),
        patch(
            "custom_components.nest_protect.NestClient.get_access_token_from_refresh_token",
            side_effect=aiohttp.ClientError(),
        ),
    ):
        await component_setup_with_refresh_token()

    assert config_entry_with_refresh_token.state is ConfigEntryState.SETUP_RETRY


async def test_authenticate_failure_with_refresh_token(
    hass,
    component_setup_with_refresh_token: ComponentSetup,
    config_entry_with_refresh_token: MockConfigEntry,
):
    """Test failure when authenticating."""
    with (
        patch("custom_components.nest_protect.Store.async_load", return_value=None),
        patch(
            "custom_components.nest_protect.NestClient.get_access_token_from_refresh_token"
        ),
        patch(
            "custom_components.nest_protect.NestClient.authenticate",
            side_effect=aiohttp.ClientError(),
        ),
    ):
        await component_setup_with_refresh_token()

    assert config_entry_with_refresh_token.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.skip(
    reason="Needs to be fixed. _async_subscribe_for_data should be cancelled when the component is unloaded."
)
async def test_init_with_cookies(
    hass,
    component_setup_with_cookies: ComponentSetup,
    config_entry_with_cookies: MockConfigEntry,
):
    """Test initialization."""
    with (
        patch(
            "custom_components.nest_protect.NestClient.get_access_token_from_cookies"
        ),
        patch("custom_components.nest_protect.NestClient.authenticate"),
        patch("custom_components.nest_protect.NestClient.get_first_data"),
        patch("custom_components.nest_protect.Store.async_load", return_value=None),
        patch("custom_components.nest_protect.Store.async_save"),
    ):
        await component_setup_with_cookies()

    assert config_entry_with_cookies.state is ConfigEntryState.LOADED


async def test_access_token_failure_with_cookies(
    hass,
    component_setup_with_cookies: ComponentSetup,
    config_entry_with_cookies: MockConfigEntry,
):
    """Test failure when getting an access token."""
    with (
        patch("custom_components.nest_protect.Store.async_load", return_value=None),
        patch(
            "custom_components.nest_protect.NestClient.get_access_token_from_cookies",
            side_effect=aiohttp.ClientError(),
        ),
    ):
        await component_setup_with_cookies()

    assert config_entry_with_cookies.state is ConfigEntryState.SETUP_RETRY


async def test_authenticate_failure_with_cookies(
    hass,
    component_setup_with_cookies: ComponentSetup,
    config_entry_with_cookies: MockConfigEntry,
):
    """Test failure when authenticating."""
    with (
        patch("custom_components.nest_protect.Store.async_load", return_value=None),
        patch(
            "custom_components.nest_protect.NestClient.get_access_token_from_cookies"
        ),
        patch(
            "custom_components.nest_protect.NestClient.authenticate",
            side_effect=aiohttp.ClientError(),
        ),
    ):
        await component_setup_with_cookies()

    assert config_entry_with_cookies.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize(
    "auth_error", [aiohttp.ClientError(), PynestException("session failed")]
)
async def test_setup_failure_persists_rotated_cookies(
    hass,
    component_setup_with_cookies: ComponentSetup,
    config_entry_with_cookies: MockConfigEntry,
    auth_error,
):
    """Cookies rotated during the token exchange survive a failed setup."""
    rotated_cookies = f"{COOKIES}; SIDCC=rotated"

    def rotate(client, issue_token, cookies):
        client.refreshed_cookies = rotated_cookies
        return MagicMock(access_token="google-token")

    with (
        patch("custom_components.nest_protect.Store.async_load", return_value=None),
        patch.object(
            NestClient,
            "get_access_token_from_cookies",
            autospec=True,
            side_effect=rotate,
        ),
        patch.object(NestClient, "authenticate", side_effect=auth_error),
    ):
        await component_setup_with_cookies()

    assert config_entry_with_cookies.state is ConfigEntryState.SETUP_RETRY
    assert config_entry_with_cookies.data[CONF_COOKIES] == rotated_cookies


@pytest.mark.parametrize(
    "error",
    [NestServiceException("503 error"), BadGatewayException("502 error")],
)
async def test_authenticate_transient_service_error_with_cookies(
    hass,
    component_setup_with_cookies: ComponentSetup,
    config_entry_with_cookies: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
):
    """Test a temporary Nest outage retries setup without logging a traceback."""
    with (
        patch("custom_components.nest_protect.Store.async_load", return_value=None),
        patch(
            "custom_components.nest_protect.NestClient.get_access_token_from_cookies"
        ),
        patch(
            "custom_components.nest_protect.NestClient.authenticate",
            side_effect=error,
        ),
    ):
        await component_setup_with_cookies()

    assert config_entry_with_cookies.state is ConfigEntryState.SETUP_RETRY
    assert "Nest service temporarily unavailable" in caplog.text
    # HA attaches the traceback itself, but only at debug level.
    assert not any(
        record.exc_info and record.levelno > logging.DEBUG for record in caplog.records
    )


async def test_startup_reuses_persisted_session(
    hass,
    component_setup_with_cookies: ComponentSetup,
    config_entry_with_cookies: MockConfigEntry,
):
    """Test that a valid persisted session skips Google re-auth."""
    future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=30)
    expires_str = future.strftime("%a, %d-%b-%Y %H:%M:%S") + " GMT"

    stored_data = {
        "nest_session": {
            "access_token": "persisted-token",
            "email": "test@test.com",
            "expires_in": expires_str,
            "userid": "user1",
            "is_superuser": False,
            "language": "en",
            "weave": {},
            "user": "user.1",
            "is_staff": False,
        },
        "transport_url": "https://transport.example.com",
    }

    with (
        patch(
            "custom_components.nest_protect.Store.async_load",
            return_value=stored_data,
        ),
        patch(
            "custom_components.nest_protect.NestClient.get_first_data"
        ) as mock_first_data,
        patch(
            "custom_components.nest_protect.NestClient.get_access_token_from_cookies"
        ) as mock_cookie_auth,
    ):
        mock_first_data.return_value = MagicMock(
            updated_buckets=[],
            service_urls={"urls": {"transport_url": "https://t.example.com"}},
        )
        await component_setup_with_cookies()

    mock_cookie_auth.assert_not_called()
    assert config_entry_with_cookies.state is ConfigEntryState.LOADED


async def test_startup_falls_through_on_expired_session(
    hass,
    component_setup_with_cookies: ComponentSetup,
    config_entry_with_cookies: MockConfigEntry,
):
    """Test that an expired persisted session triggers cookie re-auth."""
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
    expires_str = past.strftime("%a, %d-%b-%Y %H:%M:%S") + " GMT"

    stored_data = {
        "nest_session": {
            "access_token": "expired-token",
            "email": "test@test.com",
            "expires_in": expires_str,
            "userid": "user1",
            "is_superuser": False,
            "language": "en",
            "weave": {},
            "user": "user.1",
            "is_staff": False,
        },
        "transport_url": "https://transport.example.com",
    }

    with (
        patch(
            "custom_components.nest_protect.Store.async_load",
            return_value=stored_data,
        ),
        patch(
            "custom_components.nest_protect.NestClient.get_access_token_from_cookies"
        ) as mock_cookie_auth,
        patch("custom_components.nest_protect.NestClient.authenticate") as mock_auth,
        patch(
            "custom_components.nest_protect.NestClient.get_first_data"
        ) as mock_first_data,
        patch("custom_components.nest_protect.Store.async_save"),
    ):
        mock_cookie_auth.return_value = MagicMock(access_token="new-google-token")
        mock_auth.return_value = MagicMock(
            access_token="new-nest-token",
            userid="user1",
            is_expired=lambda buffer_seconds=0: False,
            to_dict=lambda: {"access_token": "new-nest-token"},
        )
        mock_first_data.return_value = MagicMock(
            updated_buckets=[],
            service_urls={"urls": {"transport_url": "https://t.example.com"}},
        )
        await component_setup_with_cookies()

    mock_cookie_auth.assert_called_once()
    assert config_entry_with_cookies.state is ConfigEntryState.LOADED


async def test_startup_falls_through_on_401_from_persisted_session(
    hass,
    component_setup_with_cookies: ComponentSetup,
    config_entry_with_cookies: MockConfigEntry,
):
    """Test that a 401 from persisted session triggers cookie re-auth."""
    future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=30)
    expires_str = future.strftime("%a, %d-%b-%Y %H:%M:%S") + " GMT"

    stored_data = {
        "nest_session": {
            "access_token": "invalid-token",
            "email": "test@test.com",
            "expires_in": expires_str,
            "userid": "user1",
            "is_superuser": False,
            "language": "en",
            "weave": {},
            "user": "user.1",
            "is_staff": False,
        },
        "transport_url": "https://transport.example.com",
    }

    with (
        patch(
            "custom_components.nest_protect.Store.async_load",
            return_value=stored_data,
        ),
        patch(
            "custom_components.nest_protect.NestClient.get_first_data",
            side_effect=[
                NotAuthenticatedException("401"),
                MagicMock(
                    updated_buckets=[],
                    service_urls={"urls": {"transport_url": "https://t.example.com"}},
                ),
            ],
        ),
        patch(
            "custom_components.nest_protect.NestClient.get_access_token_from_cookies"
        ) as mock_cookie_auth,
        patch("custom_components.nest_protect.NestClient.authenticate") as mock_auth,
        patch("custom_components.nest_protect.Store.async_save"),
    ):
        mock_cookie_auth.return_value = MagicMock(access_token="new-google-token")
        mock_auth.return_value = MagicMock(
            access_token="new-nest-token",
            userid="user1",
            is_expired=lambda buffer_seconds=0: False,
            to_dict=lambda: {"access_token": "new-nest-token"},
        )
        await component_setup_with_cookies()

    mock_cookie_auth.assert_called_once()
    assert config_entry_with_cookies.state is ConfigEntryState.LOADED


async def test_subscriber_runs_as_entry_background_task(
    hass,
    component_setup_with_cookies: ComponentSetup,
    config_entry_with_cookies: MockConfigEntry,
):
    """The subscriber is tied to the entry so HA cancels it on unload."""
    future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=30)
    stored_data = {
        "nest_session": {
            "access_token": "persisted-token",
            "email": "test@test.com",
            "expires_in": future.strftime("%a, %d-%b-%Y %H:%M:%S") + " GMT",
            "userid": "user1",
            "is_superuser": False,
            "language": "en",
            "weave": {},
            "user": "user.1",
            "is_staff": False,
        },
        "transport_url": "https://transport.example.com",
    }
    subscribed = asyncio.Event()

    async def subscribe_forever(*args):
        subscribed.set()
        await asyncio.Event().wait()

    with (
        patch(
            "custom_components.nest_protect.Store.async_load",
            return_value=stored_data,
        ),
        patch(
            "custom_components.nest_protect.NestClient.get_first_data",
            return_value=MagicMock(
                updated_buckets=[],
                service_urls={"urls": {"transport_url": "https://t.example.com"}},
            ),
        ),
        patch(
            "custom_components.nest_protect.NestClient.subscribe_for_data",
            side_effect=subscribe_forever,
        ),
        patch(
            "custom_components.nest_protect._async_observe_locks_loop",
            new=AsyncMock(),
        ),
    ):
        await component_setup_with_cookies()
        await asyncio.wait_for(subscribed.wait(), 1)

        task = config_entry_with_cookies.runtime_data.subscription_task
        assert task in config_entry_with_cookies._background_tasks

        await hass.config_entries.async_unload(config_entry_with_cookies.entry_id)

    assert task.cancelled()


async def test_resubscribe_runs_as_entry_background_task(hass):
    """Re-registered subscribers must stay tracked by the config entry."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    _make_subscriber_entry_data(hass, entry)

    async def subscribe_forever(*args):
        await asyncio.Event().wait()

    with patch(
        "custom_components.nest_protect._async_subscribe_for_data",
        side_effect=subscribe_forever,
    ):
        task = _register_subscribe_task(hass, entry, _make_subscribe_data())

    assert task is entry.runtime_data.subscription_task
    assert task in entry._background_tasks
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_command_refresh_persists_rotated_cookies(
    hass,
    component_setup_with_cookies: ComponentSetup,
    config_entry_with_cookies: MockConfigEntry,
):
    """Cookies rotated by a command-triggered session refresh reach the config entry."""
    with (
        patch("custom_components.nest_protect.Store.async_load", return_value=None),
        patch("custom_components.nest_protect.Store.async_save"),
        patch(
            "custom_components.nest_protect.NestClient.get_access_token_from_cookies"
        ),
        patch(
            "custom_components.nest_protect.NestClient.authenticate",
            return_value=MagicMock(to_dict=dict),
        ),
        patch(
            "custom_components.nest_protect.NestClient.get_first_data",
            return_value=MagicMock(
                updated_buckets=[],
                service_urls={"urls": {"transport_url": "https://t.example.com"}},
            ),
        ),
        patch(
            "custom_components.nest_protect._async_subscribe_for_data",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.nest_protect._async_observe_locks_loop",
            new_callable=AsyncMock,
        ),
    ):
        await component_setup_with_cookies()

    entry_data = config_entry_with_cookies.runtime_data
    client = entry_data.client
    rotated_cookies = f"{COOKIES}; SIDCC=rotated"

    async def rotate(*args):
        client.refreshed_cookies = rotated_cookies
        client.cookies = rotated_cookies
        client.auth = MagicMock(access_token="google-token", is_expired=lambda: False)
        return client.auth

    command = AsyncMock(side_effect=[NotAuthenticatedException(), "ok"])
    client.nest_session = MagicMock(is_expired=lambda buffer_seconds=0: False)
    client.auth = None
    with (
        patch.object(client, "get_access_token", side_effect=rotate),
        patch.object(client, "authenticate", return_value=MagicMock(to_dict=dict)),
        patch("custom_components.nest_protect.Store.async_save"),
    ):
        assert (
            await entry_data.session_manager.async_run_command(
                command, NotAuthenticatedException
            )
            == "ok"
        )

    assert config_entry_with_cookies.data[CONF_COOKIES] == rotated_cookies


def _make_subscriber_entry_data(hass, entry, consecutive_auth_failures=0):
    """Build minimal HomeAssistantNestProtectData for subscriber tests."""
    client = MagicMock()
    client.nest_session = MagicMock(is_expired=lambda buffer_seconds=0: False)
    client.refreshed_cookies = None

    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    sm = NestSessionManager(
        client,
        store,
        on_cookies_refreshed=partial(_persist_refreshed_cookies, hass, entry),
    )
    sm._consecutive_auth_failures = consecutive_auth_failures

    entry.runtime_data = HomeAssistantNestProtectData(
        devices={},
        areas={},
        client=client,
        session_manager=sm,
        grpc_lock_client=MagicMock(),
        stream_health=MagicMock(),
    )
    return client, sm


def _rotate_cookies_on_token_refresh(client, new_cookies):
    """Make the client's Google token refresh rotate cookies."""
    client.auth = None

    def rotate():
        client.refreshed_cookies = new_cookies
        client.auth = MagicMock(access_token="google-token", is_expired=lambda: False)

    client.get_access_token = AsyncMock(side_effect=rotate)


def _make_subscribe_data():
    data = MagicMock()
    data.service_urls = {"urls": {"transport_url": "https://t.example.com"}}
    data.updated_buckets = []
    return data


async def test_subscriber_timeout_resets_failure_counter(hass):
    """TimeoutError resets failure counter — it's not an auth failure."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)
    client, sm = _make_subscriber_entry_data(hass, entry, consecutive_auth_failures=2)
    client.subscribe_for_data = AsyncMock(side_effect=TimeoutError())

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch("custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert sm.consecutive_auth_failures == 0


async def test_subscriber_connect_timeout_backs_off(hass):
    """A connect timeout is an outage, unlike an idle long-poll timeout."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)
    client, sm = _make_subscriber_entry_data(hass, entry, consecutive_auth_failures=1)
    entry_data = entry.runtime_data
    client.subscribe_for_data = AsyncMock(
        side_effect=aiohttp.ConnectionTimeoutError("Connection timeout")
    )

    with (
        patch("custom_components.nest_protect._register_subscribe_task") as reschedule,
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch(
            "custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock
        ) as sleep,
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert entry_data.connection_failures == 2
    assert [c.args[0] for c in sleep.await_args_list if c.args[0]] == list(
        BACKOFF_INTERVALS[:2]
    )
    assert reschedule.call_count == 2
    # Transport outages must not count towards the reauth threshold.
    assert sm.consecutive_auth_failures == 1


async def test_subscriber_connect_timeouts_do_not_hasten_reauth(hass):
    """Connect timeouts followed by a single 401 must not start reauth."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)
    client, sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock(
        side_effect=[
            *[aiohttp.ConnectionTimeoutError("Connection timeout")]
            * (MAX_AUTH_FAILURES - 1),
            NotAuthenticatedException(),
        ]
    )
    client.auth = MagicMock(is_expired=lambda: False)
    client.authenticate = AsyncMock(return_value=MagicMock(to_dict=dict))
    sm._store.async_save = AsyncMock()

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch("custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock),
        patch.object(entry, "async_start_reauth") as mock_reauth,
    ):
        for _ in range(MAX_AUTH_FAILURES):
            await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert sm.consecutive_auth_failures == 1
    mock_reauth.assert_not_called()


@pytest.mark.parametrize(
    "outcome", [{"objects": []}, TimeoutError()], ids=["success", "long_poll"]
)
async def test_subscriber_connection_resets_connection_failures(hass, outcome):
    """Reaching the transport host clears the connect-timeout backoff."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)
    client, sm = _make_subscriber_entry_data(hass, entry)
    entry_data = entry.runtime_data
    entry_data.connection_failures = 3
    if isinstance(outcome, BaseException):
        client.subscribe_for_data = AsyncMock(side_effect=outcome)
    else:
        client.subscribe_for_data = AsyncMock(return_value=outcome)

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        # Treat the long-poll as having run its full course before timing out.
        patch("custom_components.nest_protect.SUBSCRIBE_HEALTHY_SECONDS", 0),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert entry_data.connection_failures == 0


async def test_subscriber_uses_current_transport_url(hass):
    """Subscribe to the transport URL of the current session, not the setup one."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)
    client, sm = _make_subscriber_entry_data(hass, entry)
    client.transport_url = "https://new.example.com"
    client.subscribe_for_data = AsyncMock(return_value={"objects": []})

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert client.subscribe_for_data.call_args.args[2] == "https://new.example.com"


async def test_subscriber_401_accumulates_failure_counter(hass):
    """401 path must not reset the counter — repeated 401s should reach MAX_AUTH_FAILURES."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)
    client, sm = _make_subscriber_entry_data(hass, entry, consecutive_auth_failures=0)
    client.subscribe_for_data = AsyncMock(side_effect=NotAuthenticatedException())

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch.object(sm, "async_refresh_session", new_callable=AsyncMock),
        patch("custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert sm.consecutive_auth_failures == 1


async def test_subscriber_401_persists_refreshed_cookies(hass):
    """401 path persists refreshed cookies into the config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)
    new_cookies = "SID=new-sid; HSID=new-hsid"
    client, sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock(side_effect=NotAuthenticatedException())
    _rotate_cookies_on_token_refresh(client, new_cookies)
    client.authenticate = AsyncMock(return_value=MagicMock(to_dict=dict))
    sm._store.async_save = AsyncMock()

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch("custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert entry.data.get(CONF_COOKIES) == new_cookies


async def test_subscriber_ensure_session_persists_refreshed_cookies(hass):
    """Proactive session refresh persists refreshed cookies before subscribing."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)
    new_cookies = "SID=new-sid; HSID=new-hsid"
    client, sm = _make_subscriber_entry_data(hass, entry)
    client.nest_session = None
    client.subscribe_for_data = AsyncMock(return_value={"objects": []})
    _rotate_cookies_on_token_refresh(client, new_cookies)
    client.authenticate = AsyncMock(return_value=MagicMock(to_dict=dict))
    sm._store.async_save = AsyncMock()

    with patch("custom_components.nest_protect._register_subscribe_task"):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert entry.data.get(CONF_COOKIES) == new_cookies


async def test_subscriber_failed_session_refresh_persists_rotated_cookies(hass):
    """Cookies rotated before a failed Nest session refresh are still persisted."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)
    new_cookies = "SID=new-sid; HSID=new-hsid"
    client, _ = _make_subscriber_entry_data(hass, entry)
    client.nest_session = None
    client.subscribe_for_data = AsyncMock(return_value={"objects": []})
    _rotate_cookies_on_token_refresh(client, new_cookies)
    client.authenticate = AsyncMock(side_effect=PynestException("session failed"))

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch("custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    client.subscribe_for_data.assert_not_called()
    assert entry.data.get(CONF_COOKIES) == new_cookies


async def test_subscriber_401_repeated_failures_triggers_reauth(hass):
    """Repeated 401s should reach MAX_AUTH_FAILURES and trigger reauth."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)
    client, sm = _make_subscriber_entry_data(hass, entry, consecutive_auth_failures=0)
    client.subscribe_for_data = AsyncMock(side_effect=NotAuthenticatedException())

    # Google still accepts the cookies, so the refresh itself succeeds while Nest
    # keeps rejecting the session. async_refresh_session is deliberately not
    # mocked here: it must not reset the failure counter it was called to recover.
    client.auth = MagicMock(is_expired=lambda: False)
    client.authenticate = AsyncMock(return_value=MagicMock(to_dict=dict))
    sm._store.async_save = AsyncMock()

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch("custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock),
        patch.object(entry, "async_start_reauth") as mock_reauth,
    ):
        for _ in range(MAX_AUTH_FAILURES):
            await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert sm.consecutive_auth_failures == MAX_AUTH_FAILURES
    mock_reauth.assert_called_once_with(hass)


async def test_subscriber_success_resets_failure_counter(hass):
    """A successful subscribe call must reset the failure counter to zero."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)
    client, sm = _make_subscriber_entry_data(hass, entry, consecutive_auth_failures=2)
    sm._consecutive_errors = 2
    client.subscribe_for_data = AsyncMock(return_value={"objects": []})

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert sm.consecutive_auth_failures == 0
    assert sm.consecutive_errors == 0


async def test_subscriber_unrelated_errors_do_not_trigger_reauth(hass):
    """Two non-auth errors followed by one 401 must refresh, not reauth."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    client, sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock(
        side_effect=[
            {},  # response without "objects"
            TypeError("unexpected bucket field"),
            NotAuthenticatedException(),
        ]
    )

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch.object(sm, "async_refresh_session", new_callable=AsyncMock) as refresh,
        patch("custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock),
        patch.object(entry, "async_start_reauth") as reauth,
    ):
        for _ in range(3):
            await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    reauth.assert_not_called()
    refresh.assert_awaited_once()
    assert sm.consecutive_auth_failures == 1
    assert sm.consecutive_errors == 2


async def test_subscriber_repeated_parse_errors_escalate_backoff(hass):
    """A response that keeps failing to parse backs off further each time."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    client, sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock(return_value={})

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch(
            "custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock
        ) as sleep,
        patch.object(entry, "async_start_reauth") as reauth,
    ):
        for _ in range(MAX_AUTH_FAILURES):
            await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert [call.args[0] for call in sleep.call_args_list if call.args[0]] == list(
        BACKOFF_INTERVALS[:MAX_AUTH_FAILURES]
    )
    reauth.assert_not_called()


async def test_lock_batch_clears_auth_failures_only(hass):
    """A lock batch proves the session works but says nothing about REST errors."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    _, sm = _make_subscriber_entry_data(hass, entry, consecutive_auth_failures=2)
    sm._consecutive_errors = 2

    async def observe_locks():
        yield {}

    entry.runtime_data.grpc_lock_client.observe_locks = observe_locks
    await _async_observe_locks_loop(hass, entry)

    assert sm.consecutive_auth_failures == 0
    assert sm.consecutive_errors == 2


@pytest.mark.parametrize("transport", ["subscriber", "lock"])
@pytest.mark.parametrize(
    ("refresh_error", "reauth_expected"),
    [
        (aiohttp.ClientError("offline"), False),
        (TimeoutError(), False),
        (PynestException("temporary"), False),
        (NestServiceException("unavailable"), False),
        (NotAuthenticatedException("rejected"), False),
        (BadCredentialsException("revoked"), True),
        (Exception("unexpected"), False),
        (KeyError("detail"), False),
        (TypeError("unexpected keyword argument"), False),
    ],
)
async def test_auth_recovery_handles_refresh_errors(
    hass, transport, refresh_error, reauth_expected
):
    """A failed refresh must retry or request reauth without killing the task."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    client, sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock(side_effect=NotAuthenticatedException())

    attempts = []

    async def observe_locks():
        attempts.append(True)
        if len(attempts) == 1:
            raise NestLockAuthException("rejected")
        yield {}

    entry.runtime_data.grpc_lock_client.observe_locks = observe_locks
    with (
        patch("custom_components.nest_protect._register_subscribe_task") as reschedule,
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch.object(sm, "async_refresh_session", side_effect=refresh_error),
        patch(
            "custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock
        ) as sleep,
        patch.object(entry, "async_start_reauth") as reauth,
    ):
        if transport == "subscriber":
            await _async_subscribe_for_data(hass, entry, _make_subscribe_data())
            assert reschedule.call_count == (0 if reauth_expected else 1)
        else:
            await _async_observe_locks_loop(hass, entry)
            assert len(attempts) == (1 if reauth_expected else 2)

    assert reauth.call_count == int(reauth_expected)
    assert any(call.args[0] > 0 for call in sleep.call_args_list)


@pytest.mark.parametrize("transport", ["subscriber", "lock"])
async def test_auth_recovery_cancellation_does_not_restart(hass, transport):
    """Unloading during a refresh must cancel without starting more work."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    client, sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock(side_effect=NotAuthenticatedException())

    async def observe_locks():
        yield {}
        raise NestLockAuthException("rejected")

    entry.runtime_data.grpc_lock_client.observe_locks = observe_locks
    operation = (
        _async_subscribe_for_data(hass, entry, _make_subscribe_data())
        if transport == "subscriber"
        else _async_observe_locks_loop(hass, entry)
    )
    with (
        patch("custom_components.nest_protect._register_subscribe_task") as reschedule,
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch.object(sm, "async_refresh_session", side_effect=asyncio.CancelledError()),
        patch("custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock),
        patch.object(entry, "async_start_reauth") as reauth,
        pytest.raises(asyncio.CancelledError),
    ):
        await operation

    reschedule.assert_not_called()
    reauth.assert_not_called()


def _connection_errors():
    return [
        aiohttp.ServerDisconnectedError(),
        TimeoutError(),
        aiohttp.ClientConnectorError(MagicMock(), OSError(-3, "name resolution")),
        aiohttp.ClientOSError(104, "Connection reset by peer"),
        EmptyResponseException(),
    ]


def _subscriber_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize("error", _connection_errors(), ids=lambda e: type(e).__name__)
async def test_subscriber_backs_off_on_rapid_connection_errors(hass, error):
    """Connection errors that end an attempt early back off before re-subscribing."""
    entry = _subscriber_entry(hass)
    client, sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock(side_effect=error)
    entry_data = entry.runtime_data

    with (
        patch("custom_components.nest_protect._register_subscribe_task") as register,
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch(
            "custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock
        ) as sleep,
    ):
        for _ in range(3):
            await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    delays = [call.args[0] for call in sleep.call_args_list if call.args[0] > 0]
    assert len(delays) == 3
    for delay, base in zip(delays, RECONNECT_INTERVALS, strict=False):
        assert base * 0.8 <= delay <= base * 1.2
    assert entry_data.connection_failures == 3
    assert register.call_count == 3


async def test_subscriber_resubscribes_immediately_after_long_poll(hass):
    """A long-poll that times out after running its course is not a failure."""
    entry = _subscriber_entry(hass)
    client, sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock(side_effect=TimeoutError())
    entry_data = entry.runtime_data
    entry_data.connection_failures = 2

    with (
        patch("custom_components.nest_protect._register_subscribe_task") as register,
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch(
            "custom_components.nest_protect.time.monotonic",
            side_effect=[0.0, float(SUBSCRIBE_HEALTHY_SECONDS)],
        ),
        patch(
            "custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock
        ) as sleep,
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert all(call.args[0] == 0 for call in sleep.call_args_list)
    assert entry_data.connection_failures == 0
    register.assert_called_once()


async def test_subscriber_success_resets_connection_failures(hass):
    """Receiving data clears the connection failure count."""
    entry = _subscriber_entry(hass)
    client, sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock(return_value={"objects": []})
    entry_data = entry.runtime_data
    entry_data.connection_failures = 4

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert entry_data.connection_failures == 0


async def test_subscriber_does_not_spin_while_offline(hass):
    """Regression: an unreachable host used to be retried with no delay at all."""
    entry = _subscriber_entry(hass)
    client, sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock(
        side_effect=aiohttp.ClientConnectorError(MagicMock(), OSError(-3, "offline"))
    )

    with patch.object(sm, "ensure_session", new_callable=AsyncMock):
        task = asyncio.create_task(
            _async_subscribe_for_data(hass, entry, _make_subscribe_data())
        )
        entry.runtime_data.subscription_task = task
        await asyncio.sleep(0.2)
        pending = entry.runtime_data.subscription_task
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    assert client.subscribe_for_data.await_count == 1


async def test_unloaded_entry_does_not_resubscribe(hass):
    """Without runtime data the subscriber must neither run nor reschedule."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    with patch.object(entry, "async_create_background_task") as create_task:
        assert _register_subscribe_task(hass, entry, _make_subscribe_data()) is None
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    create_task.assert_not_called()


async def _run_data_subscribes(hass, count, *, fast=True):
    """Run the subscriber `count` times with Nest answering data at once."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)
    client, sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock(return_value={"objects": []})

    with (
        patch("custom_components.nest_protect._register_subscribe_task") as reschedule,
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch(
            "custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock
        ) as sleep,
        # An interval of 0 makes every answer count as slow.
        nullcontext()
        if fast
        else patch("custom_components.nest_protect.SUBSCRIBE_MIN_INTERVAL_SECONDS", 0),
    ):
        for _ in range(count):
            await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    guard_sleeps = [c for c in sleep.await_args_list if c.args and c.args[0] == 2]
    return entry.runtime_data, guard_sleeps, reschedule


async def test_subscriber_single_fast_answer_resubscribes_at_once(hass):
    """A burst of one fast answer is normal and isn't delayed."""
    entry_data, guard_sleeps, reschedule = await _run_data_subscribes(hass, 1)

    assert entry_data.fast_data_responses == 1
    assert guard_sleeps == []
    reschedule.assert_called_once()


async def test_subscriber_spaces_out_a_run_of_fast_answers(hass):
    """Repeated instant answers (stale revisions) are throttled, not looped."""
    entry_data, guard_sleeps, reschedule = await _run_data_subscribes(hass, 5)

    assert entry_data.fast_data_responses == 5
    assert len(guard_sleeps) == 4
    assert reschedule.call_count == 5


async def test_subscriber_normal_answers_reset_the_guard(hass):
    """Answers that took a normal amount of time never trigger the guard."""
    entry_data, guard_sleeps, _ = await _run_data_subscribes(hass, 5, fast=False)

    assert entry_data.fast_data_responses == 0
    assert guard_sleeps == []


async def test_subscriber_warns_once_about_a_fast_answer_loop(hass, caplog):
    """The warning fires once when the run reaches the threshold."""
    await _run_data_subscribes(hass, SUBSCRIBE_FAST_RESPONSE_WARNING + 3)

    warnings = [r for r in caplog.records if "subscribes in a row" in r.getMessage()]
    assert len(warnings) == 1


async def test_failing_google_keep_warm_does_not_stop_updates(hass):
    """Regression: a Google hiccup must not cut off a still-valid Nest session.

    The hourly keep-warm refresh used to raise out of ensure_session(), so a
    connection reset from Google stopped Nest updates and sent the subscriber
    into its retry loop, calling Google again on every iteration.
    """
    entry = _subscriber_entry(hass)
    client, sm = _make_subscriber_entry_data(hass, entry)
    client.auth = None
    client.refreshed_cookies = None
    client.get_access_token = AsyncMock(
        side_effect=aiohttp.ClientOSError(104, "Connection reset by peer")
    )
    client.subscribe_for_data = AsyncMock(return_value={"objects": []})
    sm._google_refreshed_at = 1.0  # long overdue

    with (
        patch("custom_components.nest_protect._register_subscribe_task") as register,
        patch(
            "custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock
        ) as sleep,
        # Keeps the fast-answer guard out of the sleeps asserted on below.
        patch("custom_components.nest_protect.SUBSCRIBE_MIN_INTERVAL_SECONDS", 0),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    client.get_access_token.assert_awaited_once()
    assert client.subscribe_for_data.await_count == 2
    assert all(call.args[0] == 0 for call in sleep.call_args_list)
    assert register.call_count == 2
