"""Tests for Nest Protect diagnostics."""

from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect.const import DOMAIN
from custom_components.nest_protect.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_reuse_the_integration_session(hass):
    """Diagnostics must not sign in again and rotate cookies behind our back."""
    entry = MockConfigEntry(domain=DOMAIN, data={"issue_token": "x", "cookies": "y"})
    entry.add_to_hass(hass)

    entry_data = MagicMock()
    entry_data.session_manager.ensure_session = AsyncMock()
    entry_data.session_manager.google_refresh_diagnostics = {"attempts": 3}
    entry_data.client.get_first_data = AsyncMock()
    entry_data.client.get_access_token_from_cookies = AsyncMock()
    entry_data.client.authenticate = AsyncMock()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data

    with patch(
        "custom_components.nest_protect.diagnostics.dataclasses.asdict",
        return_value={},
    ):
        data = await async_get_config_entry_diagnostics(hass, entry)

    entry_data.session_manager.ensure_session.assert_awaited_once()
    entry_data.client.get_access_token_from_cookies.assert_not_called()
    entry_data.client.authenticate.assert_not_called()
    assert data["google_refresh"] == {"attempts": 3}
