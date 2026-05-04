"""Test init."""

import datetime
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect.pynest.exceptions import NotAuthenticatedException

from .conftest import ComponentSetup


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
