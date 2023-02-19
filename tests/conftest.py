"""Fixtures for testing."""

from collections.abc import Awaitable, Callable, Generator
from typing import TypeVar

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect.const import DOMAIN

# Typing helpers
ComponentSetup = Callable[[], Awaitable[None]]
T = TypeVar("T")
YieldFixture = Generator[T, None, None]


REFRESH_TOKEN = "some-refresh-token"
ISSUE_TOKEN = "some-issue-token"
COOKIES = "some-cookies"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations) -> None:
    """Enable custom integration."""
    yield


@pytest.fixture
async def config_entry_with_refresh_token() -> MockConfigEntry:
    """Fixture to initialize a MockConfigEntry."""
    return MockConfigEntry(domain=DOMAIN, data={"refresh_token": REFRESH_TOKEN})


@pytest.fixture
async def component_setup_with_refresh_token(
    hass: HomeAssistant,
    config_entry_with_refresh_token: MockConfigEntry,
) -> YieldFixture[ComponentSetup]:
    """Fixture for setting up the component."""
    config_entry_with_refresh_token.add_to_hass(hass)

    async def func() -> None:
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    yield func

    # Verify clean unload
    await hass.config_entries.async_unload(config_entry_with_refresh_token.entry_id)
    await hass.async_block_till_done()
    assert config_entry_with_refresh_token.state is ConfigEntryState.NOT_LOADED


@pytest.fixture
async def config_entry_with_cookies() -> MockConfigEntry:
    """Fixture to initialize a MockConfigEntry."""
    return MockConfigEntry(
        domain=DOMAIN, data={"issue_token": ISSUE_TOKEN, "cookies": COOKIES}
    )


@pytest.fixture
async def component_setup_with_cookies(
    hass: HomeAssistant,
    config_entry_with_cookies: MockConfigEntry,
) -> YieldFixture[ComponentSetup]:
    """Fixture for setting up the component."""
    config_entry_with_cookies.add_to_hass(hass)

    async def func() -> None:
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    yield func

    # Verify clean unload
    await hass.config_entries.async_unload(config_entry_with_cookies.entry_id)
    await hass.async_block_till_done()
    assert config_entry_with_cookies.state is ConfigEntryState.NOT_LOADED
