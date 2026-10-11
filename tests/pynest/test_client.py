"""Tests for NestClient."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from custom_components.nest_protect.pynest.client import (
    NestClient,
    merge_cookies,
    parse_retry_after,
)
from custom_components.nest_protect.pynest.const import NEST_REQUEST
from custom_components.nest_protect.pynest.exceptions import (
    BadCredentialsException,
    GoogleThrottledException,
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
        with pytest.raises(Exception, match="invalid_grant"):
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


async def test_get_access_token_prefers_cookies_over_legacy_refresh_token():
    """Cookies win when a re-authenticated entry still carries a refresh token.

    Regression test: preferring the refresh token meant the cookies were never
    exercised, so they went stale and Google eventually rejected the session.
    """
    nest_client = NestClient(session=MagicMock())
    nest_client.issue_token = "https://accounts.google.com/o/oauth2/iframerpc"
    nest_client.cookies = "SID=current"
    nest_client.refresh_token = "obsolete-legacy-token"

    with (
        patch.object(NestClient, "get_access_token_from_cookies") as mock_cookie_auth,
        patch.object(
            NestClient, "get_access_token_from_refresh_token"
        ) as mock_token_auth,
    ):
        await nest_client.get_access_token()

    mock_cookie_auth.assert_called_once()
    mock_token_auth.assert_not_called()


async def test_get_access_token_falls_back_to_refresh_token():
    """Entries with only a refresh token keep using it."""
    nest_client = NestClient(session=MagicMock())
    nest_client.issue_token = None
    nest_client.cookies = None
    nest_client.refresh_token = "legacy-token"

    with (
        patch.object(NestClient, "get_access_token_from_cookies") as mock_cookie_auth,
        patch.object(
            NestClient, "get_access_token_from_refresh_token"
        ) as mock_token_auth,
    ):
        await nest_client.get_access_token()

    mock_cookie_auth.assert_not_called()
    mock_token_auth.assert_called_once()


def test_merge_cookies_drops_deleted():
    """Test that merge_cookies removes names listed as deleted."""
    original = "SID=old; __Secure-1PSIDTS=stale; HSID=keep"
    result = merge_cookies(original, {"SID": "new"}, ["__Secure-1PSIDTS"])
    assert "SID=new" in result
    assert "HSID=keep" in result
    assert "__Secure-1PSIDTS" not in result


@pytest.mark.enable_socket
async def test_get_access_token_from_cookies_drops_expired_cookie(socket_enabled):
    """A Set-Cookie that deletes a cookie must not be stored as a value."""

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
        response.del_cookie("__Secure-1PSIDTS")
        return response

    app = web.Application()
    app.router.add_get("/issue-token", make_token_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        url = server.make_url("/issue-token")
        await nest_client.get_access_token_from_cookies(
            str(url), "SID=old-sid; __Secure-1PSIDTS=stale; APISID=keep-me"
        )
        assert nest_client.refreshed_cookies is not None
        assert "SID=new-sid-value" in nest_client.refreshed_cookies
        assert "APISID=keep-me" in nest_client.refreshed_cookies
        assert "__Secure-1PSIDTS" not in nest_client.refreshed_cookies
        assert nest_client.cookies == nest_client.refreshed_cookies


@pytest.mark.enable_socket
async def test_get_access_token_from_cookies_keeps_cookies_on_error(socket_enabled):
    """A logged-out response must leave the working cookies untouched."""

    async def make_token_response(request):
        response = web.json_response(
            {"error": "USER_LOGGED_OUT", "detail": "No active session found."}
        )
        response.del_cookie("SID")
        response.del_cookie("__Secure-1PSIDTS")
        return response

    app = web.Application()
    app.router.add_get("/issue-token", make_token_response)

    original = "SID=old-sid; __Secure-1PSIDTS=stale; APISID=keep-me"

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        url = server.make_url("/issue-token")
        with pytest.raises(BadCredentialsException):
            await nest_client.get_access_token_from_cookies(str(url), original)

        assert nest_client.refreshed_cookies is None
        assert nest_client.cookies == original


@pytest.mark.enable_socket
async def test_get_access_token_from_cookies_rate_limited(socket_enabled):
    """A 429 raises a throttling error carrying Retry-After, keeping rotations."""

    async def make_token_response(request):
        response = web.Response(status=429, text="Too Many Requests")
        response.headers["Retry-After"] = "120"
        response.set_cookie("SIDCC", "rotated")
        return response

    app = web.Application()
    app.router.add_get("/issue-token", make_token_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        url = server.make_url("/issue-token")
        with pytest.raises(GoogleThrottledException) as err:
            await nest_client.get_access_token_from_cookies(str(url), "SID=old")

        assert err.value.retry_after == 120
        assert nest_client.cookies == "SID=old; SIDCC=rotated"


@pytest.mark.enable_socket
async def test_get_access_token_from_cookies_html_response(socket_enabled):
    """An HTML page instead of JSON is treated as throttling."""

    async def make_token_response(request):
        return web.Response(
            status=503, text="<html>unusual traffic</html>", content_type="text/html"
        )

    app = web.Application()
    app.router.add_get("/issue-token", make_token_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        url = server.make_url("/issue-token")
        with pytest.raises(GoogleThrottledException):
            await nest_client.get_access_token_from_cookies(str(url), "SID=old")


@pytest.mark.enable_socket
async def test_get_access_token_from_cookies_error_keeps_rotation_not_deletion(
    socket_enabled,
):
    """On an error response, rotated values apply but deletions don't."""

    async def make_token_response(request):
        response = web.json_response({"error": "internal_failure"})
        response.set_cookie("SIDCC", "rotated")
        response.del_cookie("SID")
        return response

    app = web.Application()
    app.router.add_get("/issue-token", make_token_response)

    async with TestServer(app) as server, ClientSession() as session:
        nest_client = NestClient(session)
        url = server.make_url("/issue-token")
        with pytest.raises(Exception, match="internal_failure"):
            await nest_client.get_access_token_from_cookies(
                str(url), "SID=keep; SIDCC=old"
            )

        assert nest_client.cookies == "SID=keep; SIDCC=rotated"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("90", 90.0), ("not-a-date", None)],
)
def test_parse_retry_after(value, expected):
    """Retry-After is parsed from seconds; garbage is ignored."""
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date():
    """Retry-After also accepts an HTTP date."""
    future = datetime.now(UTC) + timedelta(minutes=10)
    seconds = parse_retry_after(format_datetime(future, usegmt=True))
    assert 590 <= seconds <= 600
