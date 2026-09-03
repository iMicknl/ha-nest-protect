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
    NotAuthenticatedException,
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
@pytest.mark.parametrize("status", [200, 400])
async def test_cookie_invalid_grant_is_bad_credentials(socket_enabled, status):
    """Explicit cookie rejection must be eligible for provider fallback."""

    async def make_token_response(request):
        return web.json_response(
            {"error": "invalid_grant"},
            headers=None,
            content_type="application/json",
            status=status,
        )

    app = web.Application()
    app.router.add_get("/issue-token", make_token_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        url = server.make_url("/issue-token")
        with pytest.raises(BadCredentialsException, match="invalid_grant"):
            await nest_client.get_access_token_from_cookies(str(url), "cookies")


@pytest.mark.enable_socket
async def test_refresh_token_invalid_grant_is_bad_credentials(socket_enabled):
    """An HTTP 400 invalid_grant must reject only the refresh-token provider."""

    async def make_token_response(request):
        return web.json_response({"error": "invalid_grant"}, status=400)

    app = web.Application()
    app.router.add_post("/token", make_token_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        with (
            patch(
                "custom_components.nest_protect.pynest.client.TOKEN_URL",
                server.make_url("/token"),
            ),
            pytest.raises(BadCredentialsException, match="invalid_grant"),
        ):
            await nest_client.get_access_token_from_refresh_token("refresh-token")


@pytest.mark.enable_socket
async def test_unclassified_google_forbidden_is_not_bad_credentials(socket_enabled):
    """A proxy or anti-abuse response must not trigger user reauthentication."""

    async def make_token_response(request):
        return web.Response(status=403, text="request blocked")

    app = web.Application()
    app.router.add_get("/issue-token", make_token_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        url = server.make_url("/issue-token")
        with pytest.raises(PynestException, match="403"):
            await nest_client.get_access_token_from_cookies(str(url), "cookies")


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
@pytest.mark.parametrize("status", [401, 403])
async def test_get_first_data_classifies_auth_status(status, socket_enabled):
    """Text and JSON authorization failures must use the recovery exception."""

    async def api_response(request):
        return web.Response(status=status, text="access denied")

    app = web.Application()
    app.router.add_post("/api/0.1/user/example-user/app_launch", api_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        base = str(server.make_url("")).rstrip("/")
        with (
            patch.object(nest_client.environment, "host", base),
            patch(
                "custom_components.nest_protect.pynest.client.APP_LAUNCH_URL_FORMAT",
                "{host}/api/0.1/user/{user_id}/app_launch",
            ),
            pytest.raises(NotAuthenticatedException),
        ):
            await nest_client.get_first_data("access-token", "example-user")


@pytest.mark.enable_socket
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_get_first_data_classifies_transient_status(status, socket_enabled):
    """Rate limits and server failures must never look like bad credentials."""

    async def api_response(request):
        return web.Response(status=status, text="temporarily unavailable")

    app = web.Application()
    app.router.add_post("/api/0.1/user/example-user/app_launch", api_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        base = str(server.make_url("")).rstrip("/")
        with (
            patch.object(nest_client.environment, "host", base),
            patch(
                "custom_components.nest_protect.pynest.client.APP_LAUNCH_URL_FORMAT",
                "{host}/api/0.1/user/{user_id}/app_launch",
            ),
            pytest.raises(NestServiceException),
        ):
            await nest_client.get_first_data("access-token", "example-user")


@pytest.mark.enable_socket
async def test_get_first_data_classifies_json_access_denied(socket_enabled):
    """A successful HTTP envelope can still carry an explicit auth rejection."""

    async def api_response(request):
        return web.json_response({"error": "access_denied"})

    app = web.Application()
    app.router.add_post("/api/0.1/user/example-user/app_launch", api_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        base = str(server.make_url("")).rstrip("/")
        with (
            patch.object(nest_client.environment, "host", base),
            patch(
                "custom_components.nest_protect.pynest.client.APP_LAUNCH_URL_FORMAT",
                "{host}/api/0.1/user/{user_id}/app_launch",
            ),
            pytest.raises(NotAuthenticatedException),
        ):
            await nest_client.get_first_data("access-token", "example-user")


@pytest.mark.enable_socket
@pytest.mark.parametrize("operation", ["subscribe", "update"])
@pytest.mark.parametrize(
    ("status", "expected_exception"),
    [
        (200, NotAuthenticatedException),
        (403, NotAuthenticatedException),
        (429, NestServiceException),
        (503, NestServiceException),
    ],
)
async def test_transport_operations_classify_status_before_json(
    operation, status, expected_exception, socket_enabled
):
    """REST transport failures must enter coordinated recovery."""

    async def api_response(request):
        return web.json_response({"error": "access_denied"}, status=status)

    app = web.Application()
    app.router.add_post("/v6/subscribe", api_response)
    app.router.add_post("/v6/put", api_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        transport_url = str(server.make_url("")).rstrip("/")

        if operation == "subscribe":
            request = nest_client.subscribe_for_data(
                "access-token", "example-user", transport_url, []
            )
        else:
            request = nest_client.update_objects(
                "access-token", "example-user", transport_url, {}
            )

        with pytest.raises(expected_exception):
            await request


@pytest.mark.enable_socket
@pytest.mark.parametrize(
    ("status", "expected_exception"),
    [
        (401, NotAuthenticatedException),
        (403, NotAuthenticatedException),
        (429, NestServiceException),
        (503, NestServiceException),
    ],
)
async def test_authenticate_classifies_session_status(
    status, expected_exception, socket_enabled
):
    """The Nest session endpoint must classify failures before JSON decoding."""

    async def jwt_response(request):
        return web.json_response({"jwt": "nest-jwt"})

    async def session_response(request):
        return web.Response(status=status, text="session failure")

    app = web.Application()
    app.router.add_post("/jwt", jwt_response)
    app.router.add_get("/session", session_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        base = str(server.make_url("")).rstrip("/")
        with (
            patch.object(nest_client.environment, "host", base),
            patch(
                "custom_components.nest_protect.pynest.client.NEST_AUTH_URL_JWT",
                f"{base}/jwt",
            ),
            pytest.raises(expected_exception),
        ):
            await nest_client.authenticate("google-token")


@pytest.mark.enable_socket
async def test_unknown_google_token_error_uses_pynest_exception(socket_enabled):
    """Unknown Google failures must remain transient integration failures."""

    async def make_token_response(request):
        return web.json_response({"error": "temporarily_unavailable"})

    app = web.Application()
    app.router.add_get("/issue-token", make_token_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        url = server.make_url("/issue-token")
        with pytest.raises(PynestException, match="temporarily_unavailable"):
            await nest_client.get_access_token_from_cookies(str(url), "cookies")


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
