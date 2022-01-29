"""Test init."""

from unittest.mock import patch

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import ComponentSetup

REFRESH_TOKEN = "some-token"


async def test_init(
    hass, component_setup: ComponentSetup, config_entry: MockConfigEntry
):
    """Test initialization."""
    with patch("custom_components.nest_protect.NestClient.get_access_token"), patch(
        "custom_components.nest_protect.NestClient.authenticate"
    ), patch("custom_components.nest_protect.NestClient.get_first_data"):
        await component_setup()

    assert config_entry.state is ConfigEntryState.LOADED


async def test_access_token_failure(
    hass, component_setup: ComponentSetup, config_entry: MockConfigEntry
):
    """Test failure when getting an access token."""
    with patch(
        "custom_components.nest_protect.NestClient.get_access_token",
        side_effect=aiohttp.ClientError(),
    ):
        await component_setup()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_authenticate_failure(
    hass, component_setup: ComponentSetup, config_entry: MockConfigEntry
):
    """Test failure when authenticating."""
    with patch("custom_components.nest_protect.NestClient.get_access_token"), patch(
        "custom_components.nest_protect.NestClient.authenticate",
        side_effect=aiohttp.ClientError(),
    ):
        await component_setup()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
