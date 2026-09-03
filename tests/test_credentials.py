"""Tests for Google credential providers."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.nest_protect.const import (
    CONF_COOKIES,
    CONF_ISSUE_TOKEN,
    CONF_REFRESH_TOKEN,
)
from custom_components.nest_protect.credentials import (
    CookieCredentialProvider,
    RefreshTokenCredentialProvider,
    apply_credential_updates,
    create_credential_providers,
    credential_field_names,
)
from custom_components.nest_protect.pynest.exceptions import BadCredentialsException


async def test_cookie_provider_saves_rotation_when_request_is_rejected():
    """A response-side rotation must survive a rejected token response."""
    client = MagicMock(
        issue_token="https://accounts.google.com/issue",
        cookies="SID=old",
        refreshed_cookies=None,
    )

    async def reject_credentials(*_):
        client.cookies = "SID=new"
        client.refreshed_cookies = "SID=new"
        raise BadCredentialsException("USER_LOGGED_OUT")

    client.get_access_token_from_cookies = AsyncMock(side_effect=reject_credentials)
    save_credentials = AsyncMock()
    provider = CookieCredentialProvider(client, save_credentials)

    with pytest.raises(BadCredentialsException):
        await provider.async_get_access_token()

    assert save_credentials.await_args.args[0] == {CONF_COOKIES: "SID=new"}


async def test_cookie_provider_does_not_save_unchanged_credentials():
    """An unchanged response must not cause a redundant durable write."""
    client = MagicMock(
        issue_token="https://accounts.google.com/issue",
        cookies="SID=current",
        refreshed_cookies=None,
    )
    client.get_access_token_from_cookies = AsyncMock(
        return_value=MagicMock(access_token="google-token")
    )
    save_credentials = AsyncMock()
    provider = CookieCredentialProvider(client, save_credentials)

    auth = await provider.async_get_access_token()

    assert auth.access_token == "google-token"
    save_credentials.assert_not_awaited()


async def test_refresh_token_provider_remains_supported():
    """Legacy refresh-token entries must acquire tokens independently."""
    client = MagicMock(refresh_token="legacy-refresh-token")
    client.get_access_token_from_refresh_token = AsyncMock(
        return_value=MagicMock(access_token="google-token")
    )
    save_credentials = AsyncMock()
    provider = RefreshTokenCredentialProvider(client, save_credentials)

    auth = await provider.async_get_access_token()

    assert auth.access_token == "google-token"
    client.get_access_token_from_refresh_token.assert_awaited_once_with(
        "legacy-refresh-token"
    )
    save_credentials.assert_not_awaited()


def test_provider_factory_preserves_cookie_then_token_fallback_order():
    """When both methods exist, cookies remain primary and tokens are fallback."""
    client = MagicMock(
        issue_token="https://accounts.google.com/issue",
        cookies="SID=current",
        refresh_token="legacy-refresh-token",
    )

    providers = create_credential_providers(client, AsyncMock())

    assert [provider.name for provider in providers] == ["cookies", "refresh_token"]
    assert credential_field_names(providers) == {
        CONF_ISSUE_TOKEN,
        CONF_COOKIES,
        CONF_REFRESH_TOKEN,
    }


def test_durable_updates_can_explicitly_clear_a_credential_method():
    """A future provider must be able to retire superseded credentials."""
    client = MagicMock(
        cookies="SID=old",
        issue_token="old-issue-token",
        refresh_token="old-refresh-token",
    )

    apply_credential_updates(
        client,
        {
            CONF_COOKIES: None,
            CONF_ISSUE_TOKEN: None,
            CONF_REFRESH_TOKEN: None,
        },
        {CONF_COOKIES, CONF_ISSUE_TOKEN, CONF_REFRESH_TOKEN},
    )

    assert client.cookies is None
    assert client.issue_token is None
    assert client.refresh_token is None
