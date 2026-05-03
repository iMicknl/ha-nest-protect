"""Tests for NestClient."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.nest_protect.pynest.client import NestClient
from custom_components.nest_protect.pynest.const import NEST_REQUEST


def _mock_session_get(json_data):
    """Create a mock session with a GET response returning json_data."""
    response = AsyncMock()
    response.json = AsyncMock(return_value=json_data)

    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=context)
    return session


def _mock_session_post(json_data):
    """Create a mock session with a POST response returning json_data."""
    response = AsyncMock()
    response.json = AsyncMock(return_value=json_data)

    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=context)
    return session


async def test_get_access_token_from_cookies_success():
    """Test getting an access token."""
    session = _mock_session_get(
        {
            "token_type": "Bearer",
            "access_token": "new-access-token",
            "scope": "The scope",
            "login_hint": "login-hint",
            "expires_in": 3600,
            "id_token": "",
            "session_state": {"prop": "value"},
        }
    )

    nest_client = NestClient(session)
    auth = await nest_client.get_access_token_from_cookies("issue-token", "cookies")
    assert auth.access_token == "new-access-token"


async def test_get_access_token_from_cookies_error():
    """Test failure while getting an access token."""
    session = _mock_session_get({"error": "invalid_grant"})

    nest_client = NestClient(session)
    with pytest.raises(Exception, match="invalid_grant"):
        await nest_client.get_access_token_from_cookies("issue-token", "cookies")


async def test_get_first_data_success():
    """Test getting initial data from the API."""
    json_response = {
        "updated_buckets": [],
        "service_urls": {
            "urls": {
                "rubyapi_url": "https://home.nest.com/",
                "czfe_url": "https://xxxx.transport.home.nest.com",
                "log_upload_url": "https://logsink.home.nest.com/upload/user",
                "transport_url": "https://xxxx.transport.home.nest.com",
                "weather_url": "https://apps-weather.nest.com/weather/v1?query=",
                "support_url": "https://nest.secure.force.com/support/webapp?",
                "direct_transport_url": "https://xxx.transport.home.nest.com:443",
            },
            "limits": {
                "thermostats_per_structure": 20,
                "structures": 5,
                "smoke_detectors_per_structure": 18,
                "smoke_detectors": 54,
                "thermostats": 60,
            },
            "weave": {
                "service_config": "xxxx",
                "pairing_token": "xxxx",
                "access_token": "xxxx",
            },
        },
        "weather_for_structures": {},
        "2fa_enabled": False,
    }

    session = _mock_session_post(json_response)
    nest_client = NestClient(session)

    with patch(
        "custom_components.nest_protect.pynest.client.APP_LAUNCH_URL_FORMAT",
        "/api/0.1/user/{user_id}/app_launch",
    ):
        result = await nest_client.get_first_data("access-token", "example-user")

    session.post.assert_called_once()
    call_kwargs = session.post.call_args
    assert call_kwargs.kwargs["headers"]["Authorization"] == "Basic access-token"
    assert call_kwargs.kwargs["headers"]["X-nl-user-id"] == "example-user"
    assert call_kwargs.kwargs["json"] == NEST_REQUEST
    assert result.updated_buckets == []
    assert (
        result.service_urls["urls"]["transport_url"]
        == "https://xxxx.transport.home.nest.com"
    )
