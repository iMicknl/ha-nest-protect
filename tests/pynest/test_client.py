"""Tests for NestClient."""

from unittest.mock import patch

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from custom_components.nest_protect.pynest.client import NestClient, merge_cookies
from custom_components.nest_protect.pynest.const import NEST_REQUEST
from custom_components.nest_protect.pynest.exceptions import (
    BadCredentialsException,
    NestServiceException,
    PynestException,
)


@pytest.mark.enable_socket
async def test_get_access_token_from_cookies_success(socket_enabled):
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

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        url = server.make_url("/issue-token")
        auth = await nest_client.get_access_token_from_cookies(str(url), "cookies")
        assert auth.access_token == "new-access-token"


@pytest.mark.enable_socket
async def test_get_access_token_from_cookies_error(socket_enabled):
    """Test failure while getting an access token."""

    async def make_token_response(request):
        return web.json_response(
            {"error": "invalid_grant"}, headers=None, content_type="application/json"
        )

    app = web.Application()
    app.router.add_get("/issue-token", make_token_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        url = server.make_url("/issue-token")
        with pytest.raises(BadCredentialsException, match="invalid_grant"):
            await nest_client.get_access_token_from_cookies(str(url), "cookies")


@pytest.mark.enable_socket
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (400, {"error": "USER_LOGGED_OUT"}, BadCredentialsException),
        (400, {"error": "consent_required", "detail": "x"}, BadCredentialsException),
        (403, {"error": "access_denied"}, BadCredentialsException),
        (500, {"error": "internal_failure"}, NestServiceException),
        (503, "Service Unavailable", NestServiceException),
        (429, {"error": "rate_limited"}, NestServiceException),
        (200, "<html>not json</html>", PynestException),
        (200, ["unexpected"], PynestException),
        (200, {"access_token": "token"}, PynestException),
    ],
)
async def test_get_access_token_from_cookies_classifies_failures(
    socket_enabled, status, body, expected
):
    """Google failures map to pynest exceptions so callers can tell them apart."""

    async def make_token_response(request):
        if isinstance(body, str):
            return web.Response(status=status, text=body, content_type="text/html")
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_get("/issue-token", make_token_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        url = server.make_url("/issue-token")
        with pytest.raises(expected) as exc_info:
            await nest_client.get_access_token_from_cookies(str(url), "cookies")

    assert type(exc_info.value) is expected


@pytest.mark.enable_socket
async def test_authenticate_unexpected_jwt_response(socket_enabled):
    """An unexpected JWT response shape raises a pynest exception."""

    async def make_jwt_response(request):
        return web.json_response({"jwt": "token", "unexpected": True})

    app = web.Application()
    app.router.add_post("/jwt", make_jwt_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        with (
            patch(
                "custom_components.nest_protect.pynest.client.NEST_AUTH_URL_JWT",
                str(server.make_url("/jwt")),
            ),
            pytest.raises(PynestException),
        ):
            await nest_client.authenticate("access-token")


@pytest.mark.enable_socket
async def test_get_first_data_success(socket_enabled):
    """Test getting initial data from the API."""

    async def api_response(request):
        json = await request.json()
        request.app["request"].append((request.headers, json))
        return web.json_response(
            {
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
        )

    app = web.Application()
    app.router.add_post("/api/0.1/user/example-user/app_launch", api_response)
    app["request"] = []

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        base = str(server.make_url("")).rstrip("/")
        with (
            patch.object(nest_client.environment, "host", base),
            patch(
                "custom_components.nest_protect.pynest.client.APP_LAUNCH_URL_FORMAT",
                "{host}/api/0.1/user/{user_id}/app_launch",
            ),
        ):
            result = await nest_client.get_first_data("access-token", "example-user")

    assert len(app["request"]) == 1
    (headers, json_request) = app["request"][0]
    assert headers.get("Authorization") == "Basic access-token"
    assert headers.get("X-nl-user-id") == "example-user"
    assert json_request == NEST_REQUEST
    assert result.updated_buckets == []
    assert (
        result.service_urls["urls"]["transport_url"]
        == "https://xxxx.transport.home.nest.com"
    )


@pytest.mark.enable_socket
async def test_get_access_token_from_cookies_captures_refreshed_cookies(socket_enabled):
    """Test that Set-Cookie headers from Google are captured."""

    async def make_token_response(request):
        response = web.json_response(
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
        response.set_cookie("SID", "new-sid-value")
        response.set_cookie("HSID", "new-hsid-value")
        return response

    app = web.Application()
    app.router.add_get("/issue-token", make_token_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        url = server.make_url("/issue-token")
        auth = await nest_client.get_access_token_from_cookies(
            str(url), "SID=old-sid; HSID=old-hsid; APISID=keep-me"
        )
        assert auth.access_token == "new-access-token"
        # Refreshed cookies should be stored on the client
        assert nest_client.refreshed_cookies is not None
        assert "SID=new-sid-value" in nest_client.refreshed_cookies
        assert "HSID=new-hsid-value" in nest_client.refreshed_cookies
        assert "APISID=keep-me" in nest_client.refreshed_cookies
        assert nest_client.cookies == nest_client.refreshed_cookies


@pytest.mark.enable_socket
async def test_get_access_token_no_set_cookie_headers(socket_enabled):
    """Test that refreshed_cookies is None when no Set-Cookie headers present."""

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

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        url = server.make_url("/issue-token")
        await nest_client.get_access_token_from_cookies(str(url), "SID=old-sid")
        assert nest_client.refreshed_cookies is None


def test_merge_cookies_overrides_existing():
    """Test that merge_cookies replaces existing cookie values."""
    original = "SID=old; HSID=keep; OTHER=val"
    new_cookies = {"SID": "new"}
    result = merge_cookies(original, new_cookies)
    assert "SID=new" in result
    assert "HSID=keep" in result
    assert "OTHER=val" in result


def test_merge_cookies_adds_new():
    """Test that merge_cookies adds new cookies."""
    original = "SID=old"
    new_cookies = {"NEWSID": "fresh"}
    result = merge_cookies(original, new_cookies)
    assert "SID=old" in result
    assert "NEWSID=fresh" in result


def test_merge_cookies_empty_new():
    """Test that merge_cookies returns original when no new cookies."""
    original = "SID=old; HSID=val"
    result = merge_cookies(original, {})
    assert result == original


def test_merge_cookies_drops_expired():
    """Test that merge_cookies removes cookies the server expired."""
    original = "SID=old; SIDCC=old-cc; HSID=keep"
    result = merge_cookies(original, {"SID": "new"}, expired={"SIDCC"})
    assert result == "SID=new; HSID=keep"


@pytest.mark.enable_socket
async def test_get_access_token_from_cookies_drops_expired_cookies(socket_enabled):
    """Test that cookies expired via Max-Age=0 or a past Expires are removed."""

    async def make_token_response(request):
        response = web.json_response(
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
        response.set_cookie("SID", "new-sid", max_age=63072000)
        response.set_cookie("SIDCC", "", max_age=0)
        response.set_cookie("OSID", "EXPIRED", expires="Thu, 01-Jan-1970 00:00:01 GMT")
        response.set_cookie("HSID", "new-hsid", expires="Fri, 01-Jan-2100 00:00:00 GMT")
        return response

    app = web.Application()
    app.router.add_get("/issue-token", make_token_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        url = server.make_url("/issue-token")
        await nest_client.get_access_token_from_cookies(
            str(url), "SID=old-sid; SIDCC=old-cc; OSID=old-osid; APISID=keep-me"
        )
        assert nest_client.refreshed_cookies == (
            "SID=new-sid; APISID=keep-me; HSID=new-hsid"
        )


@pytest.mark.enable_socket
async def test_get_access_token_from_cookies_all_expired(socket_enabled):
    """Test that Google expiring every cookie is a sign-out, not an empty rotation."""

    async def make_token_response(request):
        response = web.json_response(
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
        response.set_cookie("SID", "", max_age=0)
        response.set_cookie("HSID", "", expires="Thu, 01-Jan-1970 00:00:01 GMT")
        return response

    app = web.Application()
    app.router.add_get("/issue-token", make_token_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        url = server.make_url("/issue-token")
        with pytest.raises(BadCredentialsException):
            await nest_client.get_access_token_from_cookies(
                str(url), "SID=old-sid; HSID=old-hsid"
            )
        assert nest_client.refreshed_cookies is None
        assert nest_client.cookies == "SID=old-sid; HSID=old-hsid"
        assert nest_client.auth is None
