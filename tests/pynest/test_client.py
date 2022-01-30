"""Tests for NestClient."""
from unittest.mock import patch

from aiohttp import web
import pytest

from custom_components.nest_protect.pynest.client import NestClient
from custom_components.nest_protect.pynest.const import NEST_REQUEST


@pytest.mark.enable_socket
async def test_generate_token_url(aiohttp_client, loop):
    """Tests for generate_token_url."""
    app = web.Application()
    client = await aiohttp_client(app)
    nest_client = NestClient(client)
    assert nest_client.generate_token_url() == (
        "https://accounts.google.com/o/oauth2/auth/oauthchooseaccount"
        "?access_type=offline&response_type=code"
        "&scope=openid+profile+email+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fnest-account"
        "&redirect_uri=urn%3Aietf%3Awg%3Aoauth%3A2.0%3Aoob"
        "&client_id=733249279899-1gpkq9duqmdp55a7e5lft1pr2smumdla.apps.googleusercontent.com"
    )


@pytest.mark.enable_socket
async def test_get_access_token_success(aiohttp_client, loop):
    """Test getting an access token."""

    async def make_token_response(request):
        return web.json_response({"access_token": "new-access-token"})

    app = web.Application()
    app.router.add_post("/token", make_token_response)
    client = await aiohttp_client(app)

    nest_client = NestClient(client)
    with patch("custom_components.nest_protect.pynest.client.TOKEN_URL", "/token"):
        access_token = await nest_client.get_access_token("refresh-token")
        assert access_token == "new-access-token"


@pytest.mark.enable_socket
async def test_get_access_token_error(aiohttp_client, loop):
    """Test failure while getting an access token."""

    async def make_token_response(request):
        return web.json_response({"error": "invalid_grant"})

    app = web.Application()
    app.router.add_post("/token", make_token_response)
    client = await aiohttp_client(app)

    nest_client = NestClient(client)
    with patch(
        "custom_components.nest_protect.pynest.client.TOKEN_URL", "/token"
    ), pytest.raises(Exception, match="invalid_grant"):
        await nest_client.get_access_token("refresh-token")


@pytest.mark.enable_socket
async def test_get_first_data_success(aiohttp_client, loop):
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
                ]
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
        ]
    }
