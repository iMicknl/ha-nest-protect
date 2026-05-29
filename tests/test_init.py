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
    _async_subscribe_for_data,
)
from custom_components.nest_protect.const import CONF_COOKIES, MAX_AUTH_FAILURES
from custom_components.nest_protect.pynest.exceptions import NotAuthenticatedException
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
        devices={}, areas={}, client=client, session_manager=sm
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

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch.object(sm, "async_refresh_session", new_callable=AsyncMock),
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
