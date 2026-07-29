"""Tests for Nest Protect diagnostics authentication behavior."""

from unittest.mock import AsyncMock, MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect import DOMAIN, HomeAssistantNestProtectData
from custom_components.nest_protect.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.nest_protect.pynest.exceptions import (
    NotAuthenticatedException,
)
from custom_components.nest_protect.pynest.models import FirstDataAPIResponse

from .conftest import COOKIES, ISSUE_TOKEN


async def test_diagnostics_uses_the_shared_session_manager(hass):
    """Diagnostics must not bypass locking or durable credential persistence."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)
    client = MagicMock()
    client.nest_session = MagicMock(access_token="nest-token", userid="user1")
    client.get_access_token_from_cookies = AsyncMock()
    client.get_access_token_from_refresh_token = AsyncMock()
    client.authenticate = AsyncMock()
    client.get_first_data = AsyncMock(
        return_value=FirstDataAPIResponse(
            weather_for_structures={},
            service_urls={"urls": {"transport_url": "https://transport.example.com"}},
            _2fa_enabled=False,
            updated_buckets=[],
        )
    )
    session_manager = MagicMock()
    session_manager.ensure_session = AsyncMock()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = HomeAssistantNestProtectData(
        devices={},
        areas={},
        client=client,
        session_manager=session_manager,
        grpc_lock_client=MagicMock(),
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert "app_launch" in result
    session_manager.ensure_session.assert_awaited_once()
    client.get_access_token_from_cookies.assert_not_awaited()
    client.get_access_token_from_refresh_token.assert_not_awaited()
    client.authenticate.assert_not_awaited()


async def test_diagnostics_recovers_the_session_rejected_by_its_request(hass):
    """A time-valid session can still be rejected after ensure_session."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    rejected_session = MagicMock(access_token="old-token", userid="user1")
    replacement_session = MagicMock(access_token="new-token", userid="user1")
    client = MagicMock(nest_session=rejected_session)
    first_data = FirstDataAPIResponse(
        weather_for_structures={},
        service_urls={"urls": {"transport_url": "https://transport.example.com"}},
        _2fa_enabled=False,
        updated_buckets=[],
    )
    client.get_first_data = AsyncMock(
        side_effect=[NotAuthenticatedException("401"), first_data]
    )
    session_manager = MagicMock()
    session_manager.ensure_session = AsyncMock()

    async def refresh_session(**kwargs):
        client.nest_session = replacement_session
        return True

    session_manager.async_refresh_session = AsyncMock(side_effect=refresh_session)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = HomeAssistantNestProtectData(
        devices={},
        areas={},
        client=client,
        session_manager=session_manager,
        grpc_lock_client=MagicMock(),
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert "app_launch" in result
    session_manager.async_refresh_session.assert_awaited_once_with(
        rejected_session=rejected_session
    )
    assert client.get_first_data.await_count == 2
