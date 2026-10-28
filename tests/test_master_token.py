"""Tests for the Google master token auth method."""

import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect.const import DOMAIN
from custom_components.nest_protect.pynest.client import NestClient
from custom_components.nest_protect.pynest.exceptions import (
    BadCredentialsException,
    NestServiceException,
    PynestException,
)
from custom_components.nest_protect.session import NestSessionManager

EXCHANGE = (
    "custom_components.nest_protect.pynest.client.NestClient._exchange_token_sync"
)
PERFORM = "custom_components.nest_protect.pynest.client.NestClient._perform_oauth_sync"


async def test_exchange_master_token_success() -> None:
    """A Token in the gpsoauth response is stored on the client."""
    client = NestClient(session=MagicMock())

    with patch(EXCHANGE, return_value={"Token": "aas_et/master"}) as exchange:
        token = await client.exchange_master_token(
            "oauth2_4/once", "user@example.com", "abcdef0123456789"
        )

    exchange.assert_called_once_with(
        "oauth2_4/once", "user@example.com", "abcdef0123456789"
    )
    assert token == "aas_et/master"
    assert client.master_token == "aas_et/master"
    assert client.google_email == "user@example.com"
    assert client.android_id == "abcdef0123456789"


async def test_exchange_master_token_failure() -> None:
    """A response without a Token raises BadCredentialsException."""
    client = NestClient(session=MagicMock())

    with (
        patch(EXCHANGE, return_value={"Error": "BadAuthentication"}),
        pytest.raises(BadCredentialsException, match="BadAuthentication"),
    ):
        await client.exchange_master_token(
            "oauth2_4/stale", "user@example.com", "abcdef0123456789"
        )

    assert client.master_token is None


async def test_exchange_master_token_transient_error() -> None:
    """A Google error that doesn't reject the token isn't shown as invalid_auth."""
    client = NestClient(session=MagicMock())

    with (
        patch(EXCHANGE, return_value={"Error": "ServiceUnavailable"}),
        pytest.raises(NestServiceException, match="ServiceUnavailable"),
    ):
        await client.exchange_master_token(
            "oauth2_4/once", "user@example.com", "abcdef0123456789"
        )


async def test_access_token_from_master_token() -> None:
    """The minted token and its expiry become the client's auth."""
    client = NestClient(session=MagicMock())
    client.master_token = "aas_et/master"
    client.google_email = "user@example.com"
    client.android_id = "abcdef0123456789"
    expiry = int(time.time()) + 1800

    with patch(PERFORM, return_value={"Auth": "ya29.minted", "Expiry": str(expiry)}):
        auth = await client.get_access_token_from_master_token()

    assert auth.access_token == "ya29.minted"
    assert auth.id_token is None
    assert 1790 <= auth.expires_in <= 1800
    assert client.auth is auth
    assert not auth.is_expired()


async def test_access_token_from_master_token_times_out() -> None:
    """A hung gpsoauth call is a service error, so callers back off.

    A plain TimeoutError would reach the subscriber's long-poll handler, which
    resubscribes immediately.
    """
    client = NestClient(session=MagicMock())
    client.master_token = "aas_et/master"
    client.google_email = "user@example.com"
    client.android_id = "abcdef0123456789"
    release = threading.Event()

    with (
        patch(PERFORM, side_effect=lambda *args: release.wait(5)),
        patch(
            "custom_components.nest_protect.pynest.client.REQUEST_TIMEOUT_SECONDS",
            0.01,
        ),
        pytest.raises(NestServiceException, match="didn't answer"),
    ):
        await client.get_access_token_from_master_token()

    release.set()


async def test_gpsoauth_not_called_again_while_previous_call_hangs() -> None:
    """Each hung call keeps an executor thread, so don't start another one."""
    client = NestClient(session=MagicMock())
    client.master_token = "aas_et/master"
    client.google_email = "user@example.com"
    client.android_id = "abcdef0123456789"
    release = threading.Event()

    with (
        patch(PERFORM, side_effect=lambda *args: release.wait(5)) as perform,
        patch(
            "custom_components.nest_protect.pynest.client.REQUEST_TIMEOUT_SECONDS",
            0.01,
        ),
    ):
        with pytest.raises(NestServiceException):
            await client.get_access_token_from_master_token()
        with pytest.raises(NestServiceException, match="still"):
            await client.get_access_token_from_master_token()

    release.set()
    assert perform.call_count == 1


async def test_access_token_from_master_token_revoked() -> None:
    """A revoked master token raises BadCredentialsException (triggers reauth)."""
    client = NestClient(session=MagicMock())
    client.master_token = "aas_et/revoked"
    client.google_email = "user@example.com"
    client.android_id = "abcdef0123456789"

    with (
        patch(PERFORM, return_value={"Error": "BadAuthentication"}),
        pytest.raises(BadCredentialsException),
    ):
        await client.get_access_token_from_master_token()


async def test_access_token_from_master_token_transient_error() -> None:
    """A Google error that doesn't mean revocation is retried, not reauthed."""
    client = NestClient(session=MagicMock())
    client.master_token = "aas_et/master"
    client.google_email = "user@example.com"
    client.android_id = "abcdef0123456789"

    with (
        patch(PERFORM, return_value={"Error": "ServiceUnavailable"}),
        pytest.raises(NestServiceException, match="ServiceUnavailable"),
    ):
        await client.get_access_token_from_master_token()


async def test_access_token_from_master_token_network_error() -> None:
    """A network failure inside gpsoauth surfaces as a service error."""
    client = NestClient(session=MagicMock())
    client.master_token = "aas_et/master"
    client.google_email = "user@example.com"
    client.android_id = "abcdef0123456789"

    with (
        patch(
            "gpsoauth.perform_oauth",
            side_effect=requests.ConnectionError("connection reset"),
        ),
        pytest.raises(NestServiceException, match="connection reset"),
    ):
        await client.get_access_token_from_master_token()


async def test_access_token_from_master_token_incomplete() -> None:
    """Missing email or android_id asks for reauth instead of retrying forever."""
    client = NestClient(session=MagicMock())
    client.master_token = "aas_et/master"

    with patch(PERFORM) as perform, pytest.raises(BadCredentialsException):
        await client.get_access_token_from_master_token()

    perform.assert_not_called()


async def test_get_access_token_prefers_master_token() -> None:
    """With both credentials present, the master token wins over cookies."""
    client = NestClient(session=MagicMock())
    client.master_token = "aas_et/master"
    client.google_email = "user@example.com"
    client.android_id = "abcdef0123456789"
    client.issue_token = "https://accounts.google.com/issue"
    client.cookies = "SID=test"

    with (
        patch(PERFORM, return_value={"Auth": "ya29.minted"}),
        patch.object(client, "get_access_token_from_cookies") as cookies,
    ):
        auth = await client.get_access_token()

    cookies.assert_not_called()
    assert auth.access_token == "ya29.minted"


async def test_session_authenticates_with_master_token() -> None:
    """The session manager mints from the master token before trying cookies."""
    client = NestClient(session=MagicMock())
    client.master_token = "aas_et/master"
    client.issue_token = "https://accounts.google.com/issue"
    client.cookies = "SID=test"
    client.get_access_token_from_master_token = AsyncMock(
        return_value=MagicMock(access_token="ya29.minted")
    )
    client.get_access_token_from_cookies = AsyncMock()
    client.authenticate = AsyncMock(return_value=MagicMock())

    sm = NestSessionManager(client, MagicMock())
    await sm._async_authenticate_with_credentials()

    client.get_access_token_from_master_token.assert_awaited_once()
    client.get_access_token_from_cookies.assert_not_called()
    client.authenticate.assert_awaited_once_with("ya29.minted")


def _flow_client() -> MagicMock:
    client = MagicMock()
    client.master_token = "aas_et/master"
    client.exchange_master_token = AsyncMock(return_value="aas_et/master")
    client.get_access_token_from_master_token = AsyncMock(
        return_value=MagicMock(access_token="ya29.minted")
    )
    client.authenticate = AsyncMock(
        return_value=MagicMock(access_token="nest-token", userid="user1", user="user.1")
    )
    client.get_first_data = AsyncMock(
        return_value=MagicMock(
            updated_buckets=[
                MagicMock(object_key="user.1", value={"email": "user@example.com"})
            ]
        )
    )
    return client


async def test_master_token_step_creates_entry(hass: HomeAssistant) -> None:
    """The master token step stores the master token, not the one-time token."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"account_type": "production"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"method": "master_token"}
    )
    assert result["step_id"] == "master_token"

    client = _flow_client()
    with patch(
        "custom_components.nest_protect.config_flow.NestClient", return_value=client
    ) as client_cls:
        client_cls.generate_android_id.return_value = "abcdef0123456789"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "google_email": " user@example.com ",
                "oauth_token": " oauth2_4/once ",
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Nest Protect (user@example.com)"
    assert result["data"] == {
        "master_token": "aas_et/master",
        "google_email": "user@example.com",
        "android_id": "abcdef0123456789",
        "account_type": "production",
    }
    client.exchange_master_token.assert_awaited_once_with(
        "oauth2_4/once", "user@example.com", "abcdef0123456789"
    )


async def test_master_token_step_invalid_auth(hass: HomeAssistant) -> None:
    """A rejected one-time token shows invalid_auth and keeps the form open."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"account_type": "production"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"method": "master_token"}
    )

    client = _flow_client()
    client.exchange_master_token = AsyncMock(
        side_effect=BadCredentialsException("BadAuthentication")
    )
    with patch(
        "custom_components.nest_protect.config_flow.NestClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"google_email": "user@example.com", "oauth_token": "bad"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "master_token"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_with_master_token_drops_cookies(hass: HomeAssistant) -> None:
    """Reauthenticating a cookie entry swaps it to the master token."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user.1",
        version=3,
        data={
            "issue_token": "https://accounts.google.com/issue",
            "cookies": "SID=old",
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"method": "master_token"}
    )

    client = _flow_client()
    with (
        patch(
            "custom_components.nest_protect.config_flow.NestClient",
            return_value=client,
        ) as client_cls,
        patch(
            "homeassistant.config_entries.ConfigEntries.async_reload",
            return_value=True,
        ),
    ):
        client_cls.generate_android_id.return_value = "abcdef0123456789"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "google_email": "user@example.com",
                "oauth_token": "oauth2_4/once",
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {
        "master_token": "aas_et/master",
        "google_email": "user@example.com",
        "android_id": "abcdef0123456789",
        "account_type": "production",
    }


async def test_reauth_with_master_token_reuses_android_id(hass: HomeAssistant) -> None:
    """Reauth keeps the stored Android ID instead of registering a new device."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user.1",
        version=3,
        data={
            "master_token": "aas_et/revoked",
            "google_email": "user@example.com",
            "android_id": "0123456789abcdef",
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"method": "master_token"}
    )

    client = _flow_client()
    with (
        patch(
            "custom_components.nest_protect.config_flow.NestClient",
            return_value=client,
        ),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_reload",
            return_value=True,
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "google_email": "user@example.com",
                "oauth_token": "oauth2_4/once",
            },
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    client.exchange_master_token.assert_awaited_once_with(
        "oauth2_4/once", "user@example.com", "0123456789abcdef"
    )
    assert entry.data["android_id"] == "0123456789abcdef"


async def test_master_token_step_network_error(hass: HomeAssistant) -> None:
    """A network failure inside gpsoauth shows cannot_connect, not unknown."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"account_type": "production"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"method": "master_token"}
    )

    client = _flow_client()
    client.exchange_master_token = AsyncMock(
        side_effect=NestServiceException("Could not reach Google")
    )
    with patch(
        "custom_components.nest_protect.config_flow.NestClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"google_email": "user@example.com", "oauth_token": "x"},
        )

    assert result["errors"] == {"base": "cannot_connect"}


async def _start_master_token_flow(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"account_type": "production"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"method": "master_token"}
    )
    return result["flow_id"]


async def test_master_token_step_retry_reuses_master_token(
    hass: HomeAssistant,
) -> None:
    """A retry after a later step failed doesn't spend the oauth_token again.

    Google accepts each oauth_token once, so exchanging it again would fail with
    invalid_auth and force a new EmbeddedSetup capture.
    """
    flow_id = await _start_master_token_flow(hass)
    user_input = {"google_email": "user@example.com", "oauth_token": "oauth2_4/once"}

    client = _flow_client()
    client.authenticate.side_effect = [
        NestServiceException("502 error while authenticating"),
        client.authenticate.return_value,
    ]
    with patch(
        "custom_components.nest_protect.config_flow.NestClient", return_value=client
    ) as client_cls:
        client_cls.generate_android_id.side_effect = [
            "abcdef0123456789",
            "fedcba9876543210",
        ]
        result = await hass.config_entries.flow.async_configure(flow_id, user_input)
        assert result["errors"] == {"base": "cannot_connect"}

        result = await hass.config_entries.flow.async_configure(flow_id, user_input)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    client.exchange_master_token.assert_awaited_once()
    client.get_access_token_from_master_token.assert_awaited_with(
        "aas_et/master", "user@example.com", "abcdef0123456789"
    )
    assert result["data"]["android_id"] == "abcdef0123456789"


async def test_master_token_step_new_oauth_token_is_exchanged(
    hass: HomeAssistant,
) -> None:
    """A different oauth_token on retry is exchanged instead of reusing the old one."""
    flow_id = await _start_master_token_flow(hass)

    client = _flow_client()
    client.authenticate.side_effect = [
        NestServiceException("502 error while authenticating"),
        client.authenticate.return_value,
    ]
    with patch(
        "custom_components.nest_protect.config_flow.NestClient", return_value=client
    ) as client_cls:
        client_cls.generate_android_id.return_value = "abcdef0123456789"
        for oauth_token in ("oauth2_4/first", "oauth2_4/second"):
            result = await hass.config_entries.flow.async_configure(
                flow_id,
                {"google_email": "user@example.com", "oauth_token": oauth_token},
            )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert client.exchange_master_token.await_count == 2


async def test_master_token_step_logs_unexpected_pynest_error(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An unexpected error on this experimental path leaves a trace in the log."""
    flow_id = await _start_master_token_flow(hass)

    client = _flow_client()
    client.authenticate.side_effect = PynestException("unexpected Nest JWT response")
    with patch(
        "custom_components.nest_protect.config_flow.NestClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {"google_email": "user@example.com", "oauth_token": "oauth2_4/once"},
        )

    assert result["errors"] == {"base": "unknown"}
    assert "unexpected Nest JWT response" in caplog.text


async def test_master_token_step_blank_field(hass: HomeAssistant) -> None:
    """A blank field gets the generic auth error, not the extension's message."""
    flow_id = await _start_master_token_flow(hass)

    client = _flow_client()
    with patch(
        "custom_components.nest_protect.config_flow.NestClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"google_email": "user@example.com", "oauth_token": "  "}
        )

    assert result["errors"] == {"base": "invalid_auth"}
    client.exchange_master_token.assert_not_called()


async def test_master_token_step_retry_waits_for_hung_exchange(
    hass: HomeAssistant,
) -> None:
    """Retrying the form while Google hangs doesn't abandon another thread."""
    flow_id = await _start_master_token_flow(hass)
    user_input = {"google_email": "user@example.com", "oauth_token": "oauth2_4/once"}
    release = threading.Event()

    with (
        patch(EXCHANGE, side_effect=lambda *args: release.wait(5)) as exchange,
        patch(
            "custom_components.nest_protect.pynest.client.REQUEST_TIMEOUT_SECONDS",
            0.01,
        ),
    ):
        for _ in range(2):
            result = await hass.config_entries.flow.async_configure(flow_id, user_input)
            assert result["errors"] == {"base": "cannot_connect"}

    release.set()
    assert exchange.call_count == 1
