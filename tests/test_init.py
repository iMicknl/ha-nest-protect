"""Test init."""

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect import (
    DOMAIN,
    HomeAssistantNestProtectData,
    _async_mirror_credentials,
    _async_observe_locks_loop,
    _async_subscribe_for_data,
    async_remove_entry,
)
from custom_components.nest_protect.const import (
    CONF_AUTH_GENERATION,
    CONF_PREVIOUS_AUTH_GENERATION,
)
from custom_components.nest_protect.pynest.exceptions import (
    BadCredentialsException,
    NestLockAuthException,
    NotAuthenticatedException,
    PynestException,
)
from custom_components.nest_protect.session import NestSessionManager

from .conftest import COOKIES, ISSUE_TOKEN, ComponentSetup


async def test_stale_manager_cannot_mirror_credentials_after_reauth(hass):
    """A late callback from the replaced manager must not poison the new login."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "cookies": "SID=new-login",
            CONF_AUTH_GENERATION: "new-generation",
        },
    )
    entry.add_to_hass(hass)

    await _async_mirror_credentials(
        hass,
        entry,
        {
            "cookies": "SID=old-rotated",
            CONF_AUTH_GENERATION: "old-generation",
        },
    )

    assert entry.data["cookies"] == "SID=new-login"
    assert entry.data[CONF_AUTH_GENERATION] == "new-generation"


async def test_crash_handoff_can_advance_the_config_entry_generation(hass):
    """A durable reauth handoff must pass the normal generation fence."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": "SID=old",
            CONF_AUTH_GENERATION: "old-generation",
        },
    )
    entry.add_to_hass(hass)
    client = MagicMock(
        issue_token=ISSUE_TOKEN,
        cookies="SID=old",
        refresh_token=None,
        refreshed_cookies=None,
        auth=None,
        nest_session=None,
        transport_url=None,
    )
    client.get_access_token_from_cookies = AsyncMock(
        side_effect=PynestException("stop after handoff")
    )
    store = MagicMock()
    store.async_load = AsyncMock(
        return_value={
            CONF_AUTH_GENERATION: "new-generation",
            CONF_PREVIOUS_AUTH_GENERATION: "old-generation",
            "credentials": {"cookies": "SID=new-login"},
        }
    )
    store.async_save = AsyncMock()

    async def mirror_credentials(updates):
        await _async_mirror_credentials(hass, entry, updates)

    manager = NestSessionManager(
        client,
        store,
        credential_generation="old-generation",
        credential_update_callback=mirror_credentials,
        retry_delays=(),
    )

    with pytest.raises(PynestException, match="stop after handoff"):
        await manager.async_setup()

    assert entry.data[CONF_AUTH_GENERATION] == "new-generation"
    assert entry.data["cookies"] == "SID=new-login"
    assert CONF_PREVIOUS_AUTH_GENERATION not in entry.data


async def test_remove_entry_cleans_session_and_pending_reauth_stores(hass):
    """Removing an entry must delete both stores that can contain credentials."""
    entry = MockConfigEntry(domain=DOMAIN, data={})

    with patch(
        "custom_components.nest_protect.Store.async_remove",
        new_callable=AsyncMock,
    ) as remove_store:
        await async_remove_entry(hass, entry)

    assert remove_store.await_count == 2


async def test_init_with_refresh_token(
    hass,
    component_setup_with_refresh_token: ComponentSetup,
    config_entry_with_refresh_token: MockConfigEntry,
):
    """Test initialization."""
    google_auth = MagicMock(access_token="google-token")
    nest_session = MagicMock(
        access_token="nest-token",
        userid="user1",
    )
    nest_session.to_dict.return_value = {"access_token": "nest-token"}
    first_data = MagicMock(
        updated_buckets=[],
        service_urls={"urls": {"transport_url": "https://transport.example.com"}},
    )

    with (
        patch(
            "custom_components.nest_protect.NestClient.get_access_token_from_refresh_token",
            return_value=google_auth,
        ),
        patch(
            "custom_components.nest_protect.NestClient.authenticate",
            return_value=nest_session,
        ),
        patch(
            "custom_components.nest_protect.NestClient.get_first_data",
            return_value=first_data,
        ),
        patch("custom_components.nest_protect.Store.async_load", return_value=None),
        patch("custom_components.nest_protect.Store.async_save"),
        patch(
            "custom_components.nest_protect._async_subscribe_for_data",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.nest_protect._async_observe_locks_loop",
            new_callable=AsyncMock,
        ),
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


async def test_init_with_cookies(
    hass,
    component_setup_with_cookies: ComponentSetup,
    config_entry_with_cookies: MockConfigEntry,
):
    """Test initialization."""
    google_auth = MagicMock(access_token="google-token")
    nest_session = MagicMock(
        access_token="nest-token",
        userid="user1",
    )
    nest_session.to_dict.return_value = {"access_token": "nest-token"}
    first_data = MagicMock(
        updated_buckets=[],
        service_urls={"urls": {"transport_url": "https://transport.example.com"}},
    )

    with (
        patch(
            "custom_components.nest_protect.NestClient.get_access_token_from_cookies",
            return_value=google_auth,
        ),
        patch(
            "custom_components.nest_protect.NestClient.authenticate",
            return_value=nest_session,
        ),
        patch(
            "custom_components.nest_protect.NestClient.get_first_data",
            return_value=first_data,
        ),
        patch("custom_components.nest_protect.Store.async_load", return_value=None),
        patch("custom_components.nest_protect.Store.async_save"),
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


def _make_subscriber_entry_data(hass, entry):
    """Build minimal HomeAssistantNestProtectData for subscriber tests."""
    client = MagicMock()
    client.nest_session = MagicMock(is_expired=lambda buffer_seconds=0: False)

    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    sm = NestSessionManager(client, store)

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


async def test_subscriber_timeout_retries_without_reauthentication(hass):
    """A long-poll timeout is normal and must not start reauthentication."""
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
    client.subscribe_for_data = AsyncMock(side_effect=TimeoutError())

    with (
        patch("custom_components.nest_protect._register_subscribe_task") as register,
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch.object(entry, "async_start_reauth") as start_reauth,
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    register.assert_called_once()
    start_reauth.assert_not_called()


async def test_subscriber_401_recovers_the_exact_rejected_session(hass):
    """A concurrent refresh is de-duplicated against the session that failed."""
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
    client.nest_session.access_token = "rejected-token"
    rejected_session = client.nest_session
    client.subscribe_for_data = AsyncMock(side_effect=NotAuthenticatedException())

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch.object(sm, "async_refresh_session", new_callable=AsyncMock) as refresh,
        patch("custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    refresh.assert_awaited_once_with(rejected_session=rejected_session)


async def test_subscriber_bad_credentials_requests_reauth_without_escaping(hass):
    """Exhausted provider rejection must stop the subscriber and start reauth."""
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
    client.nest_session.access_token = "rejected-token"
    client.subscribe_for_data = AsyncMock(side_effect=NotAuthenticatedException())
    sm.set_reauthentication_callback(lambda: entry.async_start_reauth(hass))

    with (
        patch("custom_components.nest_protect._register_subscribe_task") as register,
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch.object(
            sm,
            "async_refresh_session",
            new_callable=AsyncMock,
            side_effect=BadCredentialsException("USER_LOGGED_OUT"),
        ),
        patch.object(entry, "async_start_reauth") as start_reauth,
        patch("custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    start_reauth.assert_called_once_with(hass)
    register.assert_not_called()


async def test_subscriber_transient_refresh_failure_retries_without_reauth(hass):
    """A transient coordinator failure must keep the entry and retry later."""
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
    client.nest_session.access_token = "rejected-token"
    client.subscribe_for_data = AsyncMock(side_effect=NotAuthenticatedException())

    with (
        patch("custom_components.nest_protect._register_subscribe_task") as register,
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch.object(
            sm,
            "async_refresh_session",
            new_callable=AsyncMock,
            side_effect=PynestException("temporary"),
        ),
        patch.object(entry, "async_start_reauth") as start_reauth,
        patch("custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    start_reauth.assert_not_called()
    register.assert_called_once()


async def test_lock_observer_recovers_the_exact_rejected_session(hass):
    """Lock and REST failures for one session share one recovery generation."""
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
    client.nest_session.access_token = "rejected-token"
    rejected_session = client.nest_session

    async def rejected_observer():
        raise NestLockAuthException("401")
        yield

    async def empty_observer():
        if False:
            yield

    grpc_client = hass.data[DOMAIN][entry.entry_id].grpc_lock_client
    grpc_client.observe_locks = MagicMock(
        side_effect=[rejected_observer(), empty_observer()]
    )

    with (
        patch.object(sm, "async_refresh_session", new_callable=AsyncMock) as refresh,
        patch("custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock),
    ):
        await _async_observe_locks_loop(hass, entry)

    refresh.assert_awaited_once_with(rejected_session=rejected_session)
