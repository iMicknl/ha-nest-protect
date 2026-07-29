"""Tests for NestSessionManager."""

from __future__ import annotations

import asyncio
import copy
import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from custom_components.nest_protect.const import (
    CONF_AUTH_GENERATION,
    CONF_PREVIOUS_AUTH_GENERATION,
    CONF_REFRESH_TOKEN,
)
from custom_components.nest_protect.pynest.client import NestClient
from custom_components.nest_protect.pynest.exceptions import (
    BadCredentialsException,
    NotAuthenticatedException,
    PynestException,
)
from custom_components.nest_protect.pynest.models import NestResponse
from custom_components.nest_protect.session import NestSessionManager


def _make_nest_response(*, expired: bool = False) -> NestResponse:
    """Create a NestResponse for testing."""
    if expired:
        dt = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
    else:
        dt = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=30)

    expires_str = dt.strftime("%a, %d-%b-%Y %H:%M:%S") + " GMT"

    return NestResponse(
        access_token="test-token",
        email="test@test.com",
        expires_in=expires_str,
        userid="user1",
        is_superuser=False,
        language="en",
        weave={},
        user="user.1",
        is_staff=False,
    )


def _make_first_data() -> MagicMock:
    """Create a mock FirstDataAPIResponse."""
    return MagicMock(
        updated_buckets=[],
        service_urls={"urls": {"transport_url": "https://transport.example.com"}},
    )


class _MemoryStore:
    """Copy values like Home Assistant's JSON-backed Store."""

    def __init__(self, data=None) -> None:
        self.data = copy.deepcopy(data)

    async def async_load(self):
        """Return stored state."""
        return copy.deepcopy(self.data)

    async def async_save(self, data) -> None:
        """Replace stored state."""
        self.data = copy.deepcopy(data)


def _make_cookie_client(cookies: str = "SID=old") -> MagicMock:
    """Create a client with explicit auth state for coordinator tests."""
    client = MagicMock()
    client.issue_token = "https://accounts.google.com/issue"
    client.cookies = cookies
    client.refresh_token = None
    client.auth = None
    client.nest_session = None
    client.transport_url = None
    client.refreshed_cookies = None
    client.get_access_token_from_cookies = AsyncMock()
    client.get_access_token_from_refresh_token = AsyncMock()
    client.authenticate = AsyncMock()
    client.get_first_data = AsyncMock(return_value=_make_first_data())
    return client


@pytest.mark.asyncio
async def test_rotation_survives_nest_failure_and_restart():
    """A Nest failure after Google rotation must not restore stale cookies."""
    first_client = _make_cookie_client()

    async def rotate_cookies(*_):
        first_client.cookies = "SID=new"
        first_client.refreshed_cookies = "SID=new"
        return MagicMock(access_token="google-token")

    first_client.get_access_token_from_cookies.side_effect = rotate_cookies
    first_client.authenticate.side_effect = PynestException("Nest unavailable")
    store = _MemoryStore()
    manager = NestSessionManager(first_client, store, retry_delays=())

    with pytest.raises(PynestException):
        await manager.async_setup()

    assert store.data["credentials"]["cookies"] == "SID=new"

    restarted_client = _make_cookie_client()
    restarted_client.get_access_token_from_cookies.return_value = MagicMock(
        access_token="google-token"
    )
    restarted_client.authenticate.side_effect = PynestException("stop after capture")
    restarted_manager = NestSessionManager(
        restarted_client,
        store,
        retry_delays=(),
    )

    with pytest.raises(PynestException):
        await restarted_manager.async_setup()

    assert (
        restarted_client.get_access_token_from_cookies.await_args.args[1] == "SID=new"
    )


@pytest.mark.asyncio
async def test_durable_update_can_retire_a_config_only_credential():
    """An explicit clear must not compare equal to an absent Store field."""
    client = _make_cookie_client()
    client.refresh_token = "legacy-refresh-token"
    update_entry = AsyncMock()
    store = _MemoryStore()
    manager = NestSessionManager(
        client,
        store,
        credential_update_callback=update_entry,
        retry_delays=(),
    )
    await manager._async_prepare_state()

    await manager._async_save_credentials({CONF_REFRESH_TOKEN: None})

    assert store.data["credentials"][CONF_REFRESH_TOKEN] is None
    assert client.refresh_token is None
    assert update_entry.await_args.args[0][CONF_REFRESH_TOKEN] is None


@pytest.mark.asyncio
async def test_future_provider_credentials_are_hydrated_without_core_field_changes():
    """A registered provider must own hydration of its declared fields."""
    client = _make_cookie_client()
    client.issue_token = None
    client.cookies = None
    client.authenticate.return_value = _make_nest_response()

    class FutureProvider:
        name = "future"
        credential_fields = frozenset({"future_token"})

        @property
        def available(self):
            return bool(getattr(client, "future_token", None))

        async def async_get_access_token(self):
            return MagicMock(access_token="future-google-token")

    manager = NestSessionManager(
        client,
        _MemoryStore(),
        initial_credentials={"future_token": "future-credential"},
        credential_providers=(FutureProvider(),),
        retry_delays=(),
    )

    assert await manager.async_setup() is not None
    assert client.future_token == "future-credential"
    client.authenticate.assert_awaited_once_with("future-google-token")


@pytest.mark.enable_socket
async def test_real_cookie_response_rotation_survives_restart(socket_enabled):
    """Compose HTTP Set-Cookie handling, provider persistence, and restart."""
    received_cookies = []

    async def issue_token_response(request):
        received_cookies.append(request.headers["cookie"])
        response = web.json_response(
            {
                "token_type": "Bearer",
                "access_token": "google-token",
                "scope": "scope",
                "login_hint": "hint",
                "expires_in": 3600,
                "id_token": "",
                "session_state": {},
            }
        )
        response.set_cookie("SID", "new")
        return response

    app = web.Application()
    app.router.add_get("/issue-token", issue_token_response)
    store = _MemoryStore()

    async with TestServer(app) as server, ClientSession() as http_session:
        issue_token = str(server.make_url("/issue-token"))
        first_client = NestClient(http_session)
        first_client.issue_token = issue_token
        first_client.cookies = "SID=old; HSID=keep"
        first_client.authenticate = AsyncMock(
            side_effect=PynestException("Nest unavailable")
        )

        with pytest.raises(PynestException):
            await NestSessionManager(first_client, store, retry_delays=()).async_setup()

        restarted_client = NestClient(http_session)
        restarted_client.issue_token = issue_token
        restarted_client.cookies = "SID=old; HSID=keep"
        restarted_client.authenticate = AsyncMock(
            side_effect=PynestException("stop after capture")
        )

        with pytest.raises(PynestException):
            await NestSessionManager(
                restarted_client, store, retry_delays=()
            ).async_setup()

    assert received_cookies == ["SID=old; HSID=keep", "SID=new; HSID=keep"]


@pytest.mark.asyncio
async def test_rotation_from_rejected_response_is_used_by_retry():
    """A rotation accompanying USER_LOGGED_OUT must feed the next attempt."""
    client = _make_cookie_client()

    async def reject_with_rotation(*_):
        if client.cookies == "SID=old":
            client.cookies = "SID=new"
            client.refreshed_cookies = "SID=new"
        raise BadCredentialsException("USER_LOGGED_OUT")

    client.get_access_token_from_cookies.side_effect = reject_with_rotation
    store = _MemoryStore()
    manager = NestSessionManager(client, store, retry_delays=(0,))

    with pytest.raises(BadCredentialsException):
        await manager.async_setup()

    assert [
        call.args[1] for call in client.get_access_token_from_cookies.await_args_list
    ] == ["SID=old", "SID=new"]
    assert store.data["credentials"]["cookies"] == "SID=new"


@pytest.mark.asyncio
async def test_cookie_rejection_falls_back_to_refresh_token():
    """An alternate configured provider must be tried before reauthentication."""
    client = _make_cookie_client()
    client.refresh_token = "legacy-refresh-token"
    client.get_access_token_from_cookies.side_effect = BadCredentialsException(
        "USER_LOGGED_OUT"
    )
    client.get_access_token_from_refresh_token.return_value = MagicMock(
        access_token="fallback-google-token"
    )
    new_session = _make_nest_response()
    client.authenticate.return_value = new_session
    store = _MemoryStore()
    manager = NestSessionManager(client, store, retry_delays=())

    result = await manager.async_setup()

    assert result is not None
    client.get_access_token_from_refresh_token.assert_awaited_once_with(
        "legacy-refresh-token"
    )
    client.authenticate.assert_awaited_once_with("fallback-google-token")


@pytest.mark.asyncio
async def test_cookie_transient_failure_falls_back_to_refresh_token():
    """An independent provider can recover without misclassifying the first."""
    client = _make_cookie_client()
    client.refresh_token = "legacy-refresh-token"
    client.get_access_token_from_cookies.side_effect = PynestException(
        "cookie endpoint changed"
    )
    client.get_access_token_from_refresh_token.return_value = MagicMock(
        access_token="fallback-google-token"
    )
    client.authenticate.return_value = _make_nest_response()
    manager = NestSessionManager(client, _MemoryStore(), retry_delays=())

    assert await manager.async_setup() is not None

    client.get_access_token_from_refresh_token.assert_awaited_once_with(
        "legacy-refresh-token"
    )
    client.authenticate.assert_awaited_once_with("fallback-google-token")


@pytest.mark.asyncio
async def test_cookie_app_launch_rejection_falls_back_to_refresh_token():
    """Each provider must get a complete Nest validation attempt."""
    client = _make_cookie_client()
    client.refresh_token = "legacy-refresh-token"
    client.get_access_token_from_cookies.return_value = MagicMock(
        access_token="cookie-google-token"
    )
    client.get_access_token_from_refresh_token.return_value = MagicMock(
        access_token="fallback-google-token"
    )
    client.authenticate.side_effect = [
        _make_nest_response(),
        _make_nest_response(),
    ]
    client.get_first_data.side_effect = [
        NotAuthenticatedException("401"),
        _make_first_data(),
    ]
    manager = NestSessionManager(client, _MemoryStore(), retry_delays=())

    assert await manager.async_setup() is not None

    client.get_access_token_from_refresh_token.assert_awaited_once_with(
        "legacy-refresh-token"
    )
    assert [call.args[0] for call in client.authenticate.await_args_list] == [
        "cookie-google-token",
        "fallback-google-token",
    ]


@pytest.mark.asyncio
async def test_cookie_nest_exchange_failure_falls_back_to_refresh_token():
    """A provider-specific pipeline failure must not skip another method."""
    client = _make_cookie_client()
    client.refresh_token = "legacy-refresh-token"
    client.get_access_token_from_cookies.return_value = MagicMock(
        access_token="cookie-google-token"
    )
    client.get_access_token_from_refresh_token.return_value = MagicMock(
        access_token="fallback-google-token"
    )
    client.authenticate.side_effect = [
        PynestException("cookie session exchange failed"),
        _make_nest_response(),
    ]
    manager = NestSessionManager(client, _MemoryStore(), retry_delays=())

    assert await manager.async_setup() is not None

    client.get_access_token_from_refresh_token.assert_awaited_once_with(
        "legacy-refresh-token"
    )


@pytest.mark.asyncio
async def test_mixed_bad_and_transient_provider_failures_do_not_start_reauth():
    """Any transient provider outcome keeps mixed evidence out of reauth."""
    client = _make_cookie_client()
    client.refresh_token = "legacy-refresh-token"
    client.get_access_token_from_cookies.side_effect = BadCredentialsException(
        "USER_LOGGED_OUT"
    )
    client.get_access_token_from_refresh_token.side_effect = PynestException(
        "temporary token endpoint failure"
    )
    manager = NestSessionManager(client, _MemoryStore(), retry_delays=(0, 0))
    start_reauth = MagicMock()
    manager.set_reauthentication_callback(start_reauth)

    with pytest.raises(PynestException, match="temporary"):
        await manager.async_setup()

    start_reauth.assert_not_called()


@pytest.mark.asyncio
async def test_reauthentication_requires_every_provider_to_reject_every_attempt():
    """Only repeated explicit provider rejection may request reauthentication."""
    client = _make_cookie_client()
    client.refresh_token = "legacy-refresh-token"
    client.get_access_token_from_cookies.side_effect = BadCredentialsException(
        "USER_LOGGED_OUT"
    )
    client.get_access_token_from_refresh_token.side_effect = BadCredentialsException(
        "invalid_grant"
    )
    store = _MemoryStore()
    manager = NestSessionManager(client, store, retry_delays=(0, 0))
    start_reauth = MagicMock()
    manager.set_reauthentication_callback(start_reauth)

    with pytest.raises(BadCredentialsException):
        await manager.async_setup()

    assert client.get_access_token_from_cookies.await_count == 3
    assert client.get_access_token_from_refresh_token.await_count == 3
    start_reauth.assert_called_once_with()


@pytest.mark.asyncio
async def test_setup_retries_session_rejected_by_app_launch():
    """A rejected new Nest session must get a fresh complete auth attempt."""
    client = _make_cookie_client()
    client.get_access_token_from_cookies.return_value = MagicMock(
        access_token="google-token"
    )
    client.authenticate.side_effect = [
        _make_nest_response(),
        _make_nest_response(),
    ]
    client.get_first_data.side_effect = [
        NotAuthenticatedException("401"),
        _make_first_data(),
    ]
    manager = NestSessionManager(client, _MemoryStore(), retry_delays=(0,))

    assert await manager.async_setup() is not None
    assert client.get_access_token_from_cookies.await_count == 2
    assert client.authenticate.await_count == 2


@pytest.mark.asyncio
async def test_nest_token_rejection_does_not_start_reauthentication():
    """A usable Google credential with Nest rejection is not a bad credential."""
    client = _make_cookie_client()
    client.get_access_token_from_cookies.return_value = MagicMock(
        access_token="google-token"
    )
    client.authenticate.side_effect = NotAuthenticatedException("401")
    manager = NestSessionManager(client, _MemoryStore(), retry_delays=(0,))
    start_reauth = MagicMock()
    manager.set_reauthentication_callback(start_reauth)

    with pytest.raises(NotAuthenticatedException):
        await manager.async_setup()

    assert client.get_access_token_from_cookies.await_count == 2
    start_reauth.assert_not_called()


@pytest.mark.asyncio
async def test_mixed_nest_and_credential_rejections_do_not_start_reauth():
    """Any usable Google token keeps mixed failures out of reauthentication."""
    client = _make_cookie_client()
    client.get_access_token_from_cookies.side_effect = [
        MagicMock(access_token="google-token"),
        BadCredentialsException("USER_LOGGED_OUT"),
        BadCredentialsException("USER_LOGGED_OUT"),
    ]
    client.authenticate.side_effect = NotAuthenticatedException("401")
    manager = NestSessionManager(client, _MemoryStore(), retry_delays=(0, 0))
    start_reauth = MagicMock()
    manager.set_reauthentication_callback(start_reauth)

    with pytest.raises(NotAuthenticatedException):
        await manager.async_setup()

    start_reauth.assert_not_called()


@pytest.mark.asyncio
async def test_unexpected_persisted_session_failure_does_not_use_credentials():
    """Malformed or unexpected Nest responses must not condemn credentials."""
    valid_session = _make_nest_response()
    store = _MemoryStore(
        {
            "nest_session": valid_session.to_dict(),
            "transport_url": "https://transport.example.com",
        }
    )
    client = _make_cookie_client()
    client.get_first_data.side_effect = PynestException("malformed response")
    manager = NestSessionManager(client, store, retry_delays=())

    with pytest.raises(PynestException):
        await manager.async_setup()

    client.get_access_token_from_cookies.assert_not_awaited()
    assert client.nest_session is None
    assert store.data["nest_session"] == valid_session.to_dict()


@pytest.mark.asyncio
async def test_concurrent_ensure_session_calls_share_one_refresh():
    """Concurrent consumers must not perform duplicate credential exchanges."""
    client = _make_cookie_client()
    token_request_started = asyncio.Event()
    release_token_request = asyncio.Event()

    async def acquire_token(*_):
        token_request_started.set()
        await release_token_request.wait()
        return MagicMock(access_token="google-token")

    client.get_access_token_from_cookies.side_effect = acquire_token
    client.authenticate.return_value = _make_nest_response()
    manager = NestSessionManager(client, _MemoryStore(), retry_delays=())

    first = asyncio.create_task(manager.ensure_session())
    await token_request_started.wait()
    second = asyncio.create_task(manager.ensure_session())
    release_token_request.set()
    await asyncio.gather(first, second)

    client.get_access_token_from_cookies.assert_awaited_once()
    client.authenticate.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_session_without_credentials_requests_reauthentication():
    """Runtime recovery without any provider must fail explicitly."""
    client = _make_cookie_client()
    client.issue_token = None
    client.cookies = None
    client.refresh_token = None
    manager = NestSessionManager(client, _MemoryStore(), retry_delays=())
    start_reauth = MagicMock()
    manager.set_reauthentication_callback(start_reauth)

    with pytest.raises(BadCredentialsException, match="No credentials"):
        await manager.ensure_session()

    start_reauth.assert_called_once_with()


@pytest.mark.asyncio
async def test_concurrent_rejections_of_same_session_share_one_refresh():
    """A second report about an already-replaced session reuses its replacement."""
    client = _make_cookie_client()
    old_session = _make_nest_response()
    old_session.access_token = "same-nest-token"
    client.nest_session = old_session
    client.get_access_token_from_cookies.return_value = MagicMock(
        access_token="google-token"
    )
    new_session = _make_nest_response()
    new_session.access_token = "same-nest-token"
    client.authenticate.return_value = new_session
    manager = NestSessionManager(client, _MemoryStore(), retry_delays=())

    await asyncio.gather(
        manager.async_refresh_session(rejected_session=old_session),
        manager.async_refresh_session(rejected_session=old_session),
    )

    client.get_access_token_from_cookies.assert_awaited_once()
    client.authenticate.assert_awaited_once()
    assert client.nest_session is new_session


@pytest.mark.asyncio
async def test_rejected_session_skips_the_google_token_that_created_it():
    """Explicit rejection must reach providers instead of looping cached auth."""
    client = _make_cookie_client()
    rejected_session = _make_nest_response()
    client.nest_session = rejected_session
    client.auth = MagicMock(access_token="cached-google-token")
    client.auth.is_expired.return_value = False
    client.get_access_token_from_cookies.return_value = MagicMock(
        access_token="fresh-google-token"
    )
    replacement = _make_nest_response()
    client.authenticate.return_value = replacement
    manager = NestSessionManager(client, _MemoryStore(), retry_delays=())

    await manager.async_refresh_session(rejected_session=rejected_session)

    client.authenticate.assert_awaited_once_with("fresh-google-token")
    assert client.nest_session is replacement


@pytest.mark.asyncio
async def test_runtime_refresh_does_not_persist_an_unvalidated_session():
    """A new session rejected by app_launch must never become durable."""
    client = _make_cookie_client()
    rejected_session = _make_nest_response()
    client.nest_session = rejected_session
    client.get_access_token_from_cookies.return_value = MagicMock(
        access_token="fresh-google-token"
    )
    candidate = _make_nest_response()

    async def install_candidate(*_):
        client.nest_session = candidate
        return candidate

    client.authenticate.side_effect = install_candidate
    client.get_first_data.side_effect = NotAuthenticatedException("401")
    store = _MemoryStore()
    manager = NestSessionManager(client, store, retry_delays=())

    with pytest.raises(NotAuthenticatedException):
        await manager.async_refresh_session(rejected_session=rejected_session)

    assert client.nest_session is None
    assert "nest_session" not in store.data


@pytest.mark.asyncio
async def test_cached_auth_rejection_clears_the_clients_candidate_session():
    """NestClient installs candidates before app_launch validation completes."""
    client = _make_cookie_client()
    client.issue_token = None
    client.cookies = None
    client.auth = MagicMock(access_token="cached-google-token")
    client.auth.is_expired.return_value = False
    candidate = _make_nest_response()

    async def install_candidate(*_):
        client.nest_session = candidate
        return candidate

    client.authenticate.side_effect = install_candidate
    client.get_first_data.side_effect = NotAuthenticatedException("401")
    manager = NestSessionManager(client, _MemoryStore(), retry_delays=())

    with pytest.raises(BadCredentialsException, match="No credentials"):
        await manager.ensure_session()

    assert client.nest_session is None


@pytest.mark.asyncio
async def test_transient_validation_failure_clears_the_clients_candidate_session():
    """A transient app_launch failure must not make its candidate reusable."""
    client = _make_cookie_client()
    client.get_access_token_from_cookies.return_value = MagicMock(
        access_token="google-token"
    )
    candidate = _make_nest_response()

    async def install_candidate(*_):
        client.nest_session = candidate
        return candidate

    client.authenticate.side_effect = install_candidate
    client.get_first_data.side_effect = PynestException("Nest unavailable")
    manager = NestSessionManager(client, _MemoryStore(), retry_delays=())

    with pytest.raises(PynestException, match="Nest unavailable"):
        await manager.ensure_session()

    assert client.nest_session is None


@pytest.mark.asyncio
async def test_rejected_replacement_can_be_invalidated_without_another_refresh():
    """A consumer's second 401 must remove the unusable durable session."""
    rejected_session = _make_nest_response()
    store = _MemoryStore(
        {
            "nest_session": rejected_session.to_dict(),
            "transport_url": "https://transport.example.com",
        }
    )
    client = _make_cookie_client()
    client.nest_session = rejected_session
    manager = NestSessionManager(client, store, retry_delays=())

    await manager.async_invalidate_session(rejected_session=rejected_session)

    assert client.nest_session is None
    assert client.auth is None
    assert "nest_session" not in store.data
    assert "transport_url" not in store.data


@pytest.mark.asyncio
async def test_ensure_session_waits_for_refresh_in_progress():
    """A valid-looking rejected session must not escape during replacement."""
    load_started = asyncio.Event()
    release_load = asyncio.Event()

    class BlockingStore(_MemoryStore):
        async def async_load(self):
            load_started.set()
            await release_load.wait()
            return await super().async_load()

    client = _make_cookie_client()
    rejected_session = _make_nest_response()
    client.nest_session = rejected_session
    client.get_access_token_from_cookies.return_value = MagicMock(
        access_token="google-token"
    )
    replacement = _make_nest_response()
    client.authenticate.return_value = replacement
    manager = NestSessionManager(client, BlockingStore(), retry_delays=())

    refresh = asyncio.create_task(
        manager.async_refresh_session(rejected_session=rejected_session)
    )
    await load_started.wait()
    ensure = asyncio.create_task(manager.ensure_session())
    await asyncio.sleep(0)

    assert not ensure.done()

    release_load.set()
    await asyncio.gather(refresh, ensure)
    assert client.nest_session is replacement


@pytest.mark.asyncio
async def test_stored_credentials_are_applied_before_session_validation():
    """The durable rotation must be live even when the Nest session is reusable."""
    valid_session = _make_nest_response()
    store = _MemoryStore(
        {
            "credentials": {"cookies": "SID=new"},
            "nest_session": valid_session.to_dict(),
            "transport_url": "https://transport.example.com",
        }
    )
    client = _make_cookie_client()

    async def validate_session(*_):
        assert client.cookies == "SID=new"
        return _make_first_data()

    client.get_first_data.side_effect = validate_session
    manager = NestSessionManager(client, store, retry_delays=())

    assert await manager.async_setup() is not None


@pytest.mark.asyncio
async def test_malformed_persisted_session_is_removed_before_fallback():
    """A corrupt expiry must not trap every setup retry on the same Store data."""
    malformed_session = _make_nest_response().to_dict()
    malformed_session["expires_in"] = "3600"
    store = _MemoryStore(
        {
            "nest_session": malformed_session,
            "transport_url": "https://transport.example.com",
        }
    )
    client = _make_cookie_client()
    client.get_access_token_from_cookies.side_effect = PynestException("stop")
    manager = NestSessionManager(client, store, retry_delays=())

    with pytest.raises(PynestException, match="stop"):
        await manager.async_setup()

    assert "nest_session" not in store.data
    assert "transport_url" not in store.data


@pytest.mark.asyncio
async def test_missing_entry_generation_adopts_durable_generation():
    """A crash before config-entry save must retain the Store generation."""
    valid_session = _make_nest_response()
    store = _MemoryStore(
        {
            CONF_AUTH_GENERATION: "durable-generation",
            "credentials": {"cookies": "SID=new"},
            "nest_session": valid_session.to_dict(),
            "transport_url": "https://transport.example.com",
        }
    )
    client = _make_cookie_client()
    update_entry = AsyncMock()
    manager = NestSessionManager(
        client,
        store,
        credential_generation=None,
        credential_update_callback=update_entry,
        retry_delays=(),
    )

    assert await manager.async_setup() is not None
    assert client.cookies == "SID=new"
    assert {CONF_AUTH_GENERATION: "durable-generation"} in [
        call.args[0] for call in update_entry.await_args_list
    ]


@pytest.mark.asyncio
async def test_new_credential_generation_discards_old_durable_auth_state():
    """A completed reauth must ignore late writes from the previous manager."""
    old_session = _make_nest_response()
    store = _MemoryStore(
        {
            CONF_AUTH_GENERATION: "old-generation",
            "credentials": {"cookies": "SID=old"},
            "nest_session": old_session.to_dict(),
            "transport_url": "https://old-transport.example.com",
        }
    )
    client = _make_cookie_client(cookies="SID=new-login")
    client.get_access_token_from_cookies.return_value = MagicMock(
        access_token="google-token"
    )
    client.authenticate.side_effect = PynestException("stop after credentials")
    manager = NestSessionManager(
        client,
        store,
        credential_generation="new-generation",
        retry_delays=(),
    )

    with pytest.raises(PynestException):
        await manager.async_setup()

    client.get_first_data.assert_not_awaited()
    assert client.get_access_token_from_cookies.await_args.args[1] == "SID=new-login"
    assert store.data == {CONF_AUTH_GENERATION: "new-generation"}


@pytest.mark.asyncio
async def test_durable_reauthentication_handoff_wins_after_a_crash():
    """A staged new login must survive before config-entry persistence."""
    store = _MemoryStore(
        {
            CONF_AUTH_GENERATION: "new-generation",
            CONF_PREVIOUS_AUTH_GENERATION: "old-generation",
            "credentials": {"cookies": "SID=new-login"},
        }
    )
    client = _make_cookie_client(cookies="SID=old")
    client.get_access_token_from_cookies.side_effect = PynestException("stop")
    update_entry = AsyncMock()
    manager = NestSessionManager(
        client,
        store,
        credential_generation="old-generation",
        credential_update_callback=update_entry,
        retry_delays=(),
    )

    with pytest.raises(PynestException, match="stop"):
        await manager.async_setup()

    assert client.cookies == "SID=new-login"
    assert {
        CONF_AUTH_GENERATION: "new-generation",
        CONF_PREVIOUS_AUTH_GENERATION: "old-generation",
    } in [call.args[0] for call in update_entry.await_args_list]


@pytest.mark.asyncio
async def test_staging_reauthentication_retires_the_old_manager():
    """The old manager must finish all writes before the new state is staged."""
    store = _MemoryStore()
    client = _make_cookie_client()
    manager = NestSessionManager(
        client,
        store,
        credential_generation="old-generation",
        retry_delays=(),
    )

    await manager.async_stage_reauthentication(
        "new-generation",
        {"cookies": "SID=new-login"},
    )

    assert store.data == {
        CONF_AUTH_GENERATION: "new-generation",
        CONF_PREVIOUS_AUTH_GENERATION: "old-generation",
        "credentials": {"cookies": "SID=new-login"},
    }
    with pytest.raises(NotAuthenticatedException, match="replaced"):
        await manager.ensure_session()


@pytest.mark.asyncio
async def test_failed_reauthentication_stage_keeps_the_old_manager_active():
    """A failed durable handoff must leave the loaded integration usable."""

    class FailingStore(_MemoryStore):
        async def async_save(self, data) -> None:
            raise OSError("disk full")

    old_session = _make_nest_response()
    client = _make_cookie_client()
    client.nest_session = old_session
    manager = NestSessionManager(
        client,
        FailingStore(),
        credential_generation="old-generation",
        retry_delays=(),
    )

    with pytest.raises(OSError, match="disk full"):
        await manager.async_stage_reauthentication(
            "new-generation",
            {"cookies": "SID=new-login"},
        )

    await manager.ensure_session()
    assert manager.current_session is old_session


@pytest.mark.asyncio
async def test_cancelled_reauthentication_stage_keeps_the_old_manager_active():
    """Cancellation before the durable handoff must not retire the manager."""
    save_started = asyncio.Event()

    class BlockingStore(_MemoryStore):
        async def async_save(self, data) -> None:
            save_started.set()
            await asyncio.Event().wait()

    old_session = _make_nest_response()
    client = _make_cookie_client()
    client.nest_session = old_session
    manager = NestSessionManager(
        client,
        BlockingStore(),
        credential_generation="old-generation",
        retry_delays=(),
    )
    staging = asyncio.create_task(
        manager.async_stage_reauthentication(
            "new-generation",
            {"cookies": "SID=new-login"},
        )
    )
    await save_started.wait()

    staging.cancel()
    with pytest.raises(asyncio.CancelledError):
        await staging

    await manager.ensure_session()
    assert manager.current_session is old_session


@pytest.mark.asyncio
async def test_staging_reauthentication_preserves_unknown_durable_state():
    """A future provider's independent Store metadata must survive reauth."""
    store = _MemoryStore({"future_provider_state": {"cursor": "opaque"}})
    manager = NestSessionManager(
        _make_cookie_client(),
        store,
        credential_generation="old-generation",
        retry_delays=(),
    )

    await manager.async_stage_reauthentication(
        "new-generation",
        {"cookies": "SID=new-login"},
    )

    assert store.data["future_provider_state"] == {"cursor": "opaque"}


@pytest.mark.asyncio
async def test_first_generation_is_durable_before_authentication():
    """An upgraded entry must save its generation before external requests."""
    client = _make_cookie_client()
    events = []

    async def update_entry(updates):
        events.append(("entry", updates))

    async def request_token(*_):
        events.append(("google", None))
        raise PynestException("stop")

    client.get_access_token_from_cookies.side_effect = request_token
    store = _MemoryStore()
    manager = NestSessionManager(
        client,
        store,
        credential_generation=None,
        credential_update_callback=update_entry,
        retry_delays=(),
    )

    with pytest.raises(PynestException):
        await manager.async_setup()

    generation = store.data[CONF_AUTH_GENERATION]
    assert generation
    assert events[0] == ("entry", {CONF_AUTH_GENERATION: generation})
    assert events[1] == ("google", None)


@pytest.mark.asyncio
async def test_restore_valid_session():
    """Test that a valid persisted session skips Google auth."""
    valid_session = _make_nest_response(expired=False)
    stored_data = {
        "nest_session": valid_session.to_dict(),
        "transport_url": "https://transport.example.com",
    }

    client = MagicMock()
    client.issue_token = "https://accounts.google.com/issue"
    client.cookies = "SID=test"
    client.refresh_token = None
    client.get_first_data = AsyncMock(return_value=_make_first_data())
    client.get_access_token_from_cookies = AsyncMock()
    client.get_access_token_from_refresh_token = AsyncMock()

    store = MagicMock()
    store.async_load = AsyncMock(return_value=stored_data)
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    result = await manager.async_setup()

    # Should return first data
    assert result is not None
    # Should NOT have called cookie auth
    client.get_access_token_from_cookies.assert_not_called()
    client.get_access_token_from_refresh_token.assert_not_called()
    # Should have called get_first_data with persisted token
    client.get_first_data.assert_called_once_with(
        valid_session.access_token, valid_session.userid
    )


@pytest.mark.asyncio
async def test_restore_expired_session_falls_through():
    """Test that an expired persisted session triggers cookie auth."""
    expired_session = _make_nest_response(expired=True)
    stored_data = {
        "nest_session": expired_session.to_dict(),
        "transport_url": "https://transport.example.com",
    }

    new_nest_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.issue_token = "https://accounts.google.com/issue"
    client.cookies = "SID=test"
    client.refresh_token = None
    client.get_access_token_from_cookies = AsyncMock(
        return_value=MagicMock(access_token="new-google-token")
    )
    client.authenticate = AsyncMock(return_value=new_nest_session)
    client.get_first_data = AsyncMock(return_value=_make_first_data())
    client.nest_session = None
    client.transport_url = None
    client.refreshed_cookies = None

    store = MagicMock()
    store.async_load = AsyncMock(return_value=stored_data)
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    result = await manager.async_setup()

    assert result is not None
    # Should have fallen through to cookie auth
    client.get_access_token_from_cookies.assert_called_once()
    client.authenticate.assert_called_once_with("new-google-token")
    assert store.async_save.await_args.args[0]["nest_session"] == (
        new_nest_session.to_dict()
    )


@pytest.mark.asyncio
async def test_restore_rejected_session_falls_through():
    """Test that a 401 from Nest on persisted session triggers cookie auth."""
    valid_session = _make_nest_response(expired=False)
    stored_data = {
        "nest_session": valid_session.to_dict(),
        "transport_url": "https://transport.example.com",
    }

    new_nest_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.issue_token = "https://accounts.google.com/issue"
    client.cookies = "SID=test"
    client.refresh_token = None
    # First call with persisted token raises 401, second call succeeds
    client.get_first_data = AsyncMock(
        side_effect=[
            NotAuthenticatedException("401"),
            _make_first_data(),
        ]
    )
    client.get_access_token_from_cookies = AsyncMock(
        return_value=MagicMock(access_token="new-google-token")
    )
    client.authenticate = AsyncMock(return_value=new_nest_session)
    client.nest_session = None
    client.transport_url = None
    client.refreshed_cookies = None

    store = MagicMock()
    store.async_load = AsyncMock(return_value=stored_data)
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    result = await manager.async_setup()

    assert result is not None
    # Should have fallen through to cookie auth after 401
    client.get_access_token_from_cookies.assert_called_once()
    client.authenticate.assert_called_once_with("new-google-token")
    assert store.async_save.await_args.args[0]["nest_session"] == (
        new_nest_session.to_dict()
    )


@pytest.mark.asyncio
async def test_no_persisted_session_uses_cookies():
    """Test that no stored data triggers cookie auth."""
    new_nest_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.issue_token = "https://accounts.google.com/issue"
    client.cookies = "SID=test"
    client.refresh_token = None
    client.get_access_token_from_cookies = AsyncMock(
        return_value=MagicMock(access_token="google-token")
    )
    client.authenticate = AsyncMock(return_value=new_nest_session)
    client.get_first_data = AsyncMock(return_value=_make_first_data())
    client.nest_session = None
    client.transport_url = None
    client.refreshed_cookies = None

    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    result = await manager.async_setup()

    assert result is not None
    # Should have used cookie auth
    client.get_access_token_from_cookies.assert_called_once()
    client.authenticate.assert_called_once_with("google-token")
    # Should have persisted the session
    store.async_save.assert_awaited()
    assert (
        store.async_save.await_args.args[0]["nest_session"]
        == new_nest_session.to_dict()
    )


@pytest.mark.asyncio
async def test_ensure_session_valid():
    """ensure_session is a no-op when session is still valid."""
    valid_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.nest_session = valid_session
    client.auth = None
    client.refresh_token = "test-refresh-token"
    client.get_access_token = AsyncMock()
    client.authenticate = AsyncMock()

    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    await manager.ensure_session()

    # Should NOT have refreshed anything
    client.get_access_token.assert_not_called()
    client.authenticate.assert_not_called()
    store.async_save.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_session_expired_refreshes():
    """ensure_session refreshes when session is expired."""
    expired_session = _make_nest_response(expired=True)
    new_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.nest_session = expired_session
    client.refresh_token = "test-refresh-token"
    client.auth = MagicMock(access_token="existing-google-token")
    client.auth.is_expired = MagicMock(return_value=False)
    client.authenticate = AsyncMock(return_value=new_session)
    client.get_first_data = AsyncMock(return_value=_make_first_data())

    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    await manager.ensure_session()

    # Should have authenticated with the existing Google token
    client.authenticate.assert_called_once_with("existing-google-token")
    # Should have persisted the new session
    store.async_save.assert_awaited()
    assert store.async_save.await_args.args[0]["nest_session"] == new_session.to_dict()
    # Should have set the new session on the client
    assert client.nest_session == new_session


@pytest.mark.asyncio
async def test_ensure_session_none_refreshes():
    """ensure_session refreshes when no session exists."""
    new_session = _make_nest_response(expired=False)

    client = MagicMock()
    client.nest_session = None
    client.auth = None
    client.issue_token = None
    client.cookies = None
    client.refresh_token = "test-refresh-token"
    client.refreshed_cookies = None
    client.get_access_token_from_refresh_token = AsyncMock(
        return_value=MagicMock(access_token="new-google-token")
    )
    client.authenticate = AsyncMock(return_value=new_session)
    client.get_first_data = AsyncMock(return_value=_make_first_data())

    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()

    manager = NestSessionManager(client=client, store=store)

    await manager.ensure_session()

    client.get_access_token_from_refresh_token.assert_awaited_once_with(
        "test-refresh-token"
    )
    # Should have authenticated with the new Google token
    client.authenticate.assert_called_once_with("new-google-token")
    # Should have persisted the new session
    store.async_save.assert_awaited()
    assert store.async_save.await_args.args[0]["nest_session"] == new_session.to_dict()
    # Should have set the new session on the client
    assert client.nest_session == new_session
