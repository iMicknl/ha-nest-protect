"""Tests for unloading a config entry."""

import asyncio
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState

from .test_device_discovery import _make_entry, _setup, _setup_data


async def test_failed_platform_unload_keeps_background_tasks(hass):
    """A platform that fails to unload leaves the entry and its tasks running."""
    entry = _make_entry(hass)
    await _setup(hass, entry, _setup_data())
    entry_data = entry.runtime_data
    subscription = hass.async_create_background_task(
        asyncio.Event().wait(), "test subscription"
    )
    entry_data.subscription_task = subscription

    with patch.object(
        hass.config_entries, "async_unload_platforms", return_value=False
    ):
        assert not await hass.config_entries.async_unload(entry.entry_id)

    assert entry.state is ConfigEntryState.FAILED_UNLOAD
    assert entry.reason == "Failed to unload one or more platforms"
    assert not subscription.done()

    subscription.cancel()
    entry_data.stream_health.async_stop()


async def test_unload_cancels_background_tasks(hass):
    """Unloading stops the subscriber."""
    entry = _make_entry(hass)
    await _setup(hass, entry, _setup_data())
    subscription = hass.async_create_background_task(
        asyncio.Event().wait(), "test subscription"
    )
    entry.runtime_data.subscription_task = subscription

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert subscription.cancelled()
