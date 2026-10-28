"""Tests for diagnostics."""

from __future__ import annotations

import datetime
import json
from contextlib import ExitStack, contextmanager
from functools import partial
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect import (
    HomeAssistantNestProtectData,
    _persist_refreshed_cookies,
)
from custom_components.nest_protect.const import CONF_COOKIES, DOMAIN
from custom_components.nest_protect.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.nest_protect.pynest.client import NestClient
from custom_components.nest_protect.pynest.const import FULL_NEST_REQUEST
from custom_components.nest_protect.pynest.exceptions import BadCredentialsException
from custom_components.nest_protect.pynest.lock_models import LockBoltState, LockState
from custom_components.nest_protect.pynest.models import (
    Bucket,
    FirstDataAPIResponse,
    NestResponse,
)
from custom_components.nest_protect.session import NestSessionManager

from .conftest import COOKIES, ISSUE_TOKEN, REFRESH_TOKEN

NEST_ACCESS_TOKEN = "nest-secret-access-token"
WEAVE_TOKEN = "weave-secret-token"
USER_ID = "8675309"
EMAIL = "someone@example.com"
TRANSPORT_URL = "https://transport.example.com"
SERIAL_A = "06AA01AC00000001"
SERIAL_B = "06AA01AC00000002"
LOCK_SERIAL = "LOCKSERIAL0001"
MAC_A = "18B4300000A1"
STREET = "1 Infinite Loop"


def _nest_session(*, expired: bool = False) -> NestResponse:
    delta = datetime.timedelta(minutes=-10 if expired else 30)
    expires = datetime.datetime.now(datetime.UTC) + delta
    return NestResponse(
        access_token=NEST_ACCESS_TOKEN,
        email=EMAIL,
        expires_in=expires.strftime("%a, %d-%b-%Y %H:%M:%S") + " GMT",
        userid=USER_ID,
        is_superuser=False,
        language="en_US",
        weave={"access_token": WEAVE_TOKEN},
        user=f"user.{USER_ID}",
        is_staff=False,
    )


def _topaz(device_id: str, serial: str) -> Bucket:
    return Bucket(
        object_key=f"topaz.{device_id}",
        object_revision=1,
        object_timestamp=1,
        value={
            "serial_number": serial,
            "wifi_mac_address": MAC_A,
            "smoke_status": 0,
            "battery_level": 5400,
            "where_id": "where-1",
        },
    )


def _lock() -> LockState:
    return LockState(
        resource_id="DEVICE_LOCK1",
        name="Front Door",
        serial_number=LOCK_SERIAL,
        bolt_state=LockBoltState.LOCKED,
        software_version="1.2-7",
        battery_level=87.0,
        location="Hallway",
    )


def _app_launch() -> FirstDataAPIResponse:
    return FirstDataAPIResponse(
        weather_for_structures={},
        service_urls={
            "urls": {"transport_url": "https://other-transport.example.com"},
            "weave": {"access_token": WEAVE_TOKEN},
        },
        _2fa_enabled=False,
        updated_buckets=[
            {
                "object_key": "topaz.AAA",
                "object_revision": 2,
                "object_timestamp": 2,
                "value": {"serial_number": SERIAL_A, "smoke_status": 0},
            },
            {
                "object_key": "widget_track.AAA",
                "object_revision": 2,
                "object_timestamp": 2,
                "value": {"online": True},
            },
            {
                "object_key": "topaz.BBB",
                "object_revision": 2,
                "object_timestamp": 2,
                "value": {"serial_number": SERIAL_B, "smoke_status": 0},
            },
            {
                "object_key": "widget_track.BBB",
                "object_revision": 2,
                "object_timestamp": 2,
                "value": {"online": False},
            },
            {
                "object_key": f"user.{USER_ID}",
                "object_revision": 2,
                "object_timestamp": 2,
                "value": {"email": EMAIL, "name": "Someone"},
            },
            {
                "object_key": "structure.s1",
                "object_revision": 2,
                "object_timestamp": 2,
                "value": {
                    "address_lines": [STREET],
                    "members": [{"user": f"user.{USER_ID}", "roles": ["owner"]}],
                },
            },
        ],
    )


def _entry(hass, data: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=(
            data
            if data is not None
            else {
                "issue_token": ISSUE_TOKEN,
                "cookies": COOKIES,
                "account_type": "production",
            }
        ),
    )
    entry.add_to_hass(hass)
    return entry


def _load(
    hass,
    entry: MockConfigEntry,
    *,
    nest_session: NestResponse | None,
    locks: dict[str, LockState] | None = None,
) -> NestClient:
    client = NestClient(session=MagicMock())
    client.issue_token = entry.data.get("issue_token")
    client.cookies = entry.data.get("cookies")
    client.refresh_token = entry.data.get("refresh_token")
    client.nest_session = nest_session
    client.transport_url = TRANSPORT_URL

    store = MagicMock()
    store.async_save = AsyncMock()

    devices = {
        b.object_key: b for b in (_topaz("AAA", SERIAL_A), _topaz("BBB", SERIAL_B))
    }
    entry.runtime_data = HomeAssistantNestProtectData(
        devices=devices,
        areas={"where-1": "Kitchen"},
        client=client,
        session_manager=NestSessionManager(
            client,
            store,
            on_cookies_refreshed=partial(_persist_refreshed_cookies, hass, entry),
        ),
        grpc_lock_client=MagicMock(),
        stream_health=MagicMock(),
        lock_state_cache=locks or {},
    )
    return client


async def _fake_first_data(self, nest_access_token, user_id, request=None):
    # The real method records the transport URL on whichever client runs it.
    self.transport_url = "https://other-transport.example.com"
    return _app_launch()


@contextmanager
def _patched_client(**overrides):
    """Patch every network-touching NestClient method and yield the mocks by name."""
    defaults = {"get_first_data": {"side_effect": _fake_first_data}}
    with ExitStack() as stack:
        yield {
            name: stack.enter_context(
                patch.object(
                    NestClient,
                    name,
                    autospec=True,
                    **overrides.get(name, defaults.get(name, {})),
                )
            )
            for name in (
                "get_first_data",
                "get_access_token_from_cookies",
                "get_access_token_from_refresh_token",
                "authenticate",
            )
        }


async def test_config_entry_diagnostics_keeps_live_session(hass):
    """Downloading diagnostics must not sign in again or touch client state."""
    entry = _entry(hass)
    session = _nest_session()
    client = _load(hass, entry, nest_session=session)
    client.refreshed_cookies = None

    with _patched_client() as mocks:
        result = await async_get_config_entry_diagnostics(hass, entry)

    mocks["get_access_token_from_cookies"].assert_not_called()
    mocks["get_access_token_from_refresh_token"].assert_not_called()
    mocks["authenticate"].assert_not_called()
    mock_first_data = mocks["get_first_data"]
    assert client.nest_session is session
    assert client.transport_url == TRANSPORT_URL
    assert client.cookies == COOKIES
    assert client.refreshed_cookies is None

    mock_first_data.assert_called_once()
    used_client, token, user_id = mock_first_data.call_args.args
    assert used_client is not client
    assert (token, user_id) == (NEST_ACCESS_TOKEN, USER_ID)
    assert mock_first_data.call_args.kwargs["request"] == FULL_NEST_REQUEST

    assert result["app_launch_error"] is None
    assert result["session"]["expired"] is False
    assert set(result["devices"]) == {"topaz.AAA", "topaz.BBB"}


async def test_diagnostics_redact_secrets_and_identifiers(hass):
    """No credential, token or personal identifier may leak into the output."""
    entry = _entry(
        hass,
        {
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "refresh_token": REFRESH_TOKEN,
            "account_type": "production",
        },
    )
    _load(hass, entry, nest_session=_nest_session(), locks={"DEVICE_LOCK1": _lock()})
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, SERIAL_A)}
    )

    with _patched_client():
        entry_result = await async_get_config_entry_diagnostics(hass, entry)
        device_result = await async_get_device_diagnostics(hass, entry, device)

    for result in (entry_result, device_result):
        dumped = json.dumps(result)
        for secret in (
            COOKIES,
            "test-sid",
            ISSUE_TOKEN,
            REFRESH_TOKEN,
            NEST_ACCESS_TOKEN,
            WEAVE_TOKEN,
            USER_ID,
            EMAIL,
            SERIAL_A,
            SERIAL_B,
            LOCK_SERIAL,
            MAC_A,
            STREET,
        ):
            assert secret not in dumped, secret

    # Operational state stays readable.
    assert entry_result["locks"][0]["bolt_state"] == LockBoltState.LOCKED
    assert entry_result["locks"][0]["battery_level"] == 87.0
    assert entry_result["devices"]["topaz.AAA"]["value"]["battery_level"] == 5400
    assert entry_result["config_entry"]["account_type"] == "production"


async def test_device_diagnostics_scoped_to_device(hass):
    """Device diagnostics only contain buckets belonging to that device."""
    entry = _entry(hass)
    _load(hass, entry, nest_session=_nest_session())
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, SERIAL_A)},
        model="Topaz-2.7",
        sw_version="3.1.4",
    )

    with _patched_client():
        result = await async_get_device_diagnostics(hass, entry, device)

    assert result["device"]["model"] == "Topaz-2.7"
    assert result["device"]["sw_version"] == "3.1.4"
    assert result["bucket"]["object_key"] == "topaz.AAA"
    assert {b["object_key"] for b in result["related_buckets"]} == {
        "topaz.AAA",
        "widget_track.AAA",
    }
    assert "app_launch" not in result
    assert "BBB" not in json.dumps(result)


async def test_lock_device_diagnostics_include_lock_state(hass):
    """Lock devices report their cached lock state without contacting Nest."""
    entry = _entry(hass)
    _load(hass, entry, nest_session=_nest_session(), locks={"DEVICE_LOCK1": _lock()})
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, LOCK_SERIAL)}
    )

    with _patched_client() as mocks:
        result = await async_get_device_diagnostics(hass, entry, device)

    mocks["get_first_data"].assert_not_called()
    assert result["lock"]["resource_id"] == "DEVICE_LOCK1"
    assert result["lock"]["bolt_state"] == LockBoltState.LOCKED
    assert result["lock"]["software_version"] == "1.2-7"
    assert "bucket" not in result


async def test_diagnostics_without_credentials(hass):
    """Missing credentials are reported instead of raising."""
    entry = _entry(hass, {"account_type": "production"})
    _load(hass, entry, nest_session=None)

    with _patched_client() as mocks:
        result = await async_get_config_entry_diagnostics(hass, entry)

    mocks["get_first_data"].assert_not_called()
    assert result["app_launch"] is None
    assert result["app_launch_error"] == "no_valid_session"
    assert result["session"]["expires_in"] is None
    assert set(result["devices"]) == {"topaz.AAA", "topaz.BBB"}


async def test_diagnostics_auth_error_is_reported(hass):
    """An auth failure while refreshing degrades to held data only."""
    entry = _entry(hass)
    client = _load(hass, entry, nest_session=_nest_session(expired=True))

    with _patched_client(
        get_access_token_from_cookies={
            "side_effect": BadCredentialsException("USER_LOGGED_OUT")
        }
    ) as mocks:
        result = await async_get_config_entry_diagnostics(hass, entry)

    mocks["get_first_data"].assert_not_called()
    assert result["app_launch"] is None
    assert result["app_launch_error"] == "BadCredentialsException"
    assert result["session"]["expired"] is True
    assert client.transport_url == TRANSPORT_URL


async def test_diagnostics_refresh_goes_through_session_manager(hass):
    """An expired session is refreshed by the session manager and rotated cookies are kept."""
    entry = _entry(hass)
    client = _load(hass, entry, nest_session=_nest_session(expired=True))
    fresh_session = _nest_session()

    async def fake_cookie_auth(self, issue_token, cookies):
        self.refreshed_cookies = "SID=rotated"
        self.cookies = self.refreshed_cookies
        self.auth = MagicMock(access_token="google-token", is_expired=lambda: False)
        return self.auth

    with _patched_client(
        get_access_token_from_cookies={"side_effect": fake_cookie_auth},
        authenticate={"return_value": fresh_session},
    ):
        result = await async_get_config_entry_diagnostics(hass, entry)

    entry_data = entry.runtime_data
    entry_data.session_manager._store.async_save.assert_awaited_once()
    assert client.nest_session is fresh_session
    assert entry.data[CONF_COOKIES] == "SID=rotated"
    assert result["app_launch_error"] is None
    assert result["session"]["expired"] is False
    assert result["session"]["expires_in"] == fresh_session.expires_in


async def test_diagnostics_for_unloaded_entry(hass):
    """An entry that is not loaded still yields redacted entry data."""
    entry = _entry(hass)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["config_entry"]["cookies"] == "**REDACTED**"
    assert result["config_entry"]["issue_token"] == "**REDACTED**"
    assert COOKIES not in json.dumps(result)
