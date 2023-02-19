"""Test init."""

from unittest.mock import patch

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import ComponentSetup


async def test_init_with_refresh_token(
    hass,
    component_setup_with_refresh_token: ComponentSetup,
    config_entry_with_refresh_token: MockConfigEntry,
):
    """Test initialization."""
    with patch(
        "custom_components.nest_protect.NestClient.get_access_token_from_refresh_token"
    ), patch("custom_components.nest_protect.NestClient.authenticate"), patch(
        "custom_components.nest_protect.NestClient.get_first_data"
    ):
        await component_setup_with_refresh_token()

    assert config_entry_with_refresh_token.state is ConfigEntryState.LOADED


async def test_access_token_failure_with_refresh_token(
    hass,
    component_setup_with_refresh_token: ComponentSetup,
    config_entry_with_refresh_token: MockConfigEntry,
):
    """Test failure when getting an access token."""
    with patch(
        "custom_components.nest_protect.NestClient.get_access_token_from_refresh_token",
        side_effect=aiohttp.ClientError(),
    ):
        await component_setup_with_refresh_token()

    assert config_entry_with_refresh_token.state is ConfigEntryState.SETUP_RETRY


async def test_authenticate_failure_with_refresh_token(
    hass,
    component_setup_with_refresh_token: ComponentSetup,
    config_entry_with_refresh_token: MockConfigEntry,
):
    """Test failure when authenticating."""
    with patch(
        "custom_components.nest_protect.NestClient.get_access_token_from_refresh_token"
    ), patch(
        "custom_components.nest_protect.NestClient.authenticate",
        side_effect=aiohttp.ClientError(),
    ):
        await component_setup_with_refresh_token()

    assert config_entry_with_refresh_token.state is ConfigEntryState.SETUP_RETRY


async def test_init_with_cookies(
    hass,
    component_setup_with_cookies: ComponentSetup,
    config_entry_with_cookies: MockConfigEntry,
):
    """Test initialization."""
    with patch(
        "custom_components.nest_protect.NestClient.get_access_token_from_cookies"
    ), patch("custom_components.nest_protect.NestClient.authenticate"), patch(
        "custom_components.nest_protect.NestClient.get_first_data"
    ):
        await component_setup_with_cookies()

    assert config_entry_with_cookies.state is ConfigEntryState.LOADED


async def test_access_token_failure_with_cookies(
    hass,
    component_setup_with_cookies: ComponentSetup,
    config_entry_with_cookies: MockConfigEntry,
):
    """Test failure when getting an access token."""
    with patch(
        "custom_components.nest_protect.NestClient.get_access_token_from_refresh_token",
        side_effect=aiohttp.ClientError(),
    ):
        await component_setup_with_cookies()

    assert config_entry_with_cookies.state is ConfigEntryState.SETUP_RETRY


async def test_authenticate_failure_with_cookies(
    hass,
    component_setup_with_cookies: ComponentSetup,
    config_entry_with_cookies: MockConfigEntry,
):
    """Test failure when authenticating."""
    with patch(
        "custom_components.nest_protect.NestClient.get_access_token_from_refresh_token"
    ), patch(
        "custom_components.nest_protect.NestClient.authenticate",
        side_effect=aiohttp.ClientError(),
    ):
        await component_setup_with_cookies()

    assert config_entry_with_cookies.state is ConfigEntryState.SETUP_RETRY
