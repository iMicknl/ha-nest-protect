"""Test init."""

import asyncio
import datetime
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
)
from custom_components.nest_protect.const import (
    CONF_COOKIES,
    MAX_AUTH_FAILURES,
    RECONNECT_INTERVALS,
    SUBSCRIBE_HEALTHY_SECONDS,
)
from custom_components.nest_protect.pynest.exceptions import (
    BadCredentialsException,
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


def _make_subscriber_entry_data(
    hass, entry, consecutive_failures=0, refreshed_cookies=None
):
    """Build minimal HomeAssistantNestProtectData for subscriber tests."""
    client = MagicMock()
    client.nest_session = MagicMock(is_expired=lambda buffer_seconds=0: False)
    client.refreshed_cookies = refreshed_cookies

    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    sm = NestSessionManager(client, store)
    sm._consecutive_failures = consecutive_failures

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = HomeAssistantNestProtectData(
        devices={},
        areas={},
        client=client,
        session_manager=sm,
        grpc_lock_client=MagicMock(),
    )
    return client, sm


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
    client, sm = _make_subscriber_entry_data(hass, entry, consecutive_failures=2)
    client.subscribe_for_data = AsyncMock(side_effect=TimeoutError())

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch("custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert sm.consecutive_failures == 0


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
    client, sm = _make_subscriber_entry_data(hass, entry, consecutive_failures=0)
    client.subscribe_for_data = AsyncMock(side_effect=NotAuthenticatedException())

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch.object(sm, "async_refresh_session", new_callable=AsyncMock),
        patch("custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert sm.consecutive_failures == 1


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
    client, sm = _make_subscriber_entry_data(hass, entry, refreshed_cookies=new_cookies)
    client.subscribe_for_data = AsyncMock(side_effect=NotAuthenticatedException())

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch.object(sm, "async_refresh_session", new_callable=AsyncMock),
        patch("custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert entry.data.get(CONF_COOKIES) == new_cookies
    # In-memory client must also be updated so the next refresh in this HA
    # session uses fresh cookies (regression guard for bc05166).
    assert client.cookies == new_cookies


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
    client, sm = _make_subscriber_entry_data(hass, entry, refreshed_cookies=new_cookies)
    client.subscribe_for_data = AsyncMock(return_value={"objects": []})

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert entry.data.get(CONF_COOKIES) == new_cookies
    assert client.cookies == new_cookies


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
    client, sm = _make_subscriber_entry_data(hass, entry, consecutive_failures=0)
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

    assert sm.consecutive_failures == MAX_AUTH_FAILURES
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
    client, sm = _make_subscriber_entry_data(hass, entry, consecutive_failures=2)
    client.subscribe_for_data = AsyncMock(return_value={"objects": []})

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert sm.consecutive_failures == 0


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

    entry_data = hass.data[DOMAIN][entry.entry_id]
    entry_data.grpc_lock_client.observe_locks = observe_locks
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

    hass.data[DOMAIN][entry.entry_id].grpc_lock_client.observe_locks = observe_locks
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
    client, _sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock(side_effect=error)
    entry_data = hass.data[DOMAIN][entry.entry_id]

    with (
        patch("custom_components.nest_protect._register_subscribe_task") as register,
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
    client, _sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock(side_effect=TimeoutError())
    entry_data = hass.data[DOMAIN][entry.entry_id]
    entry_data.connection_failures = 2

    with (
        patch("custom_components.nest_protect._register_subscribe_task") as register,
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
    client, _sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock(return_value={"objects": []})
    entry_data = hass.data[DOMAIN][entry.entry_id]
    entry_data.connection_failures = 4

    with patch("custom_components.nest_protect._register_subscribe_task"):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    assert entry_data.connection_failures == 0


async def test_subscriber_does_not_spin_while_offline(hass):
    """Regression: an unreachable host used to be retried with no delay at all."""
    entry = _subscriber_entry(hass)
    client, _sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock(
        side_effect=aiohttp.ClientConnectorError(MagicMock(), OSError(-3, "offline"))
    )

    task = asyncio.create_task(
        _async_subscribe_for_data(hass, entry, _make_subscribe_data())
    )
    hass.data[DOMAIN][entry.entry_id].subscription_task = task
    await asyncio.sleep(0.2)
    hass.data[DOMAIN][entry.entry_id].subscription_task.cancel()

    assert client.subscribe_for_data.await_count == 1
