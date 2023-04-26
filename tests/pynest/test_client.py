"""Tests for NestClient."""
from unittest.mock import patch

from aiohttp import web
import pytest

from custom_components.nest_protect.pynest.client import NestClient
from custom_components.nest_protect.pynest.const import NEST_REQUEST


@pytest.mark.enable_socket
async def test_get_access_token_from_cookies_success(
    socket_enabled, aiohttp_client, loop
):
    """Test getting an access token."""

    async def make_token_response(request):
        return web.json_response(
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

    app = web.Application()
    app.router.add_get("/issue-token", make_token_response)
    client = await aiohttp_client(app)

    nest_client = NestClient(client)
    auth = await nest_client.get_access_token_from_cookies("issue-token", "cookies")
    assert auth.access_token == "new-access-token"


@pytest.mark.enable_socket
async def test_get_access_token_from_cookies_error(
    socket_enabled, aiohttp_client, loop
):
    """Test failure while getting an access token."""

    async def make_token_response(request):
        return web.json_response(
            {"error": "invalid_grant"}, headers=None, content_type="application/json"
        )

    app = web.Application()
    app.router.add_get("/issue-token", make_token_response)
    client = await aiohttp_client(app)

    nest_client = NestClient(client)
    with pytest.raises(Exception, match="invalid_grant"):
        await nest_client.get_access_token_from_cookies("issue-token", "cookies")


@pytest.mark.enable_socket
async def test_get_first_data_success(socket_enabled, aiohttp_client, loop):
    """Test getting initial data from the API."""

    async def api_response(request):
        json = await request.json()
        request.app["request"].append((request.headers, json))
        return web.json_response(
            {
                "updated_buckets": [
                    {
                        "object_key": "example-object-key",
                    }
                ],
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
            }
        )

    app = web.Application()
    app.router.add_post("/api/0.1/user/example-user/app_launch", api_response)
    app["request"] = []
    client = await aiohttp_client(app)

    nest_client = NestClient(client)
    with patch(
        "custom_components.nest_protect.pynest.client.APP_LAUNCH_URL_FORMAT",
        "/api/0.1/user/{user_id}/app_launch",
    ):
        result = await nest_client.get_first_data("access-token", "example-user")

    assert len(app["request"]) == 1
    (headers, json_request) = app["request"][0]
    assert headers.get("Authorization") == "Basic access-token"
    assert headers.get("X-nl-user-id") == "example-user"
    assert json_request == NEST_REQUEST
    assert result == {
        "updated_buckets": [
            {
                "object_key": "example-object-key",
            }
        ],
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
    }
