"""Tests for config entry migration."""

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant.common import MockConfigEntry

from custom_components.nest_protect.config_flow import ConfigFlow
from custom_components.nest_protect.const import DOMAIN

from .conftest import COOKIES, ISSUE_TOKEN, REFRESH_TOKEN


async def _setup(hass, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    with patch("custom_components.nest_protect.async_setup_entry", return_value=True):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_migrate_v1_to_current(hass):
    """A v1 entry gains the production account type and reaches the current version."""
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, data={"refresh_token": REFRESH_TOKEN}
    )
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == ConfigFlow.VERSION
    assert entry.data == {
        "refresh_token": REFRESH_TOKEN,
        "account_type": "production",
    }


async def test_migrate_v2_to_v3_keeps_data(hass):
    """v2 refresh-token entries keep working, so only the version changes."""
    data = {"refresh_token": REFRESH_TOKEN, "account_type": "production"}
    entry = MockConfigEntry(domain=DOMAIN, version=2, data=data)
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 3
    assert entry.data == data


async def test_current_entry_is_not_migrated(hass):
    """Entries already at the current version skip migration entirely."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=ConfigFlow.VERSION,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    with patch("custom_components.nest_protect.async_migrate_entry") as migrate:
        await _setup(hass, entry)

    migrate.assert_not_called()
    assert entry.state is ConfigEntryState.LOADED
