"""Fixtures for testing."""

from typing import Any, TypeVar

import pytest
from collections.abc import Callable, Awaitable, Generator
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components.nest_protect.const import DOMAIN


# Typing helpers
ComponentSetup = Callable[[], Awaitable[None]]
T = TypeVar("T")
YieldFixture = Generator[T, None, None]


REFRESH_TOKEN = "some-token"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations) -> None:
    yield


@pytest.fixture
async def config_entry() -> MockConfigEntry:
    """Fixture to initialize a MockConfigEntry."""
    return MockConfigEntry(domain=DOMAIN, data={"refresh_token": REFRESH_TOKEN})


@pytest.fixture
async def component_setup(
    hass: HomeAssistant, config_entry: MockConfigEntry,
) -> YieldFixture[ComponentSetup]:
    """Fixture for setting up the component."""
    config_entry.add_to_hass(hass)

    async def func() -> None:
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    yield func

    # Verify clean unload
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED
