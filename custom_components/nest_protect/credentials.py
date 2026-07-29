"""Credential providers for Google access tokens."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from typing import Any, Protocol

from .const import CONF_COOKIES, CONF_ISSUE_TOKEN, CONF_REFRESH_TOKEN
from .pynest.client import NestClient
from .pynest.models import GoogleAuthResponse

CredentialUpdateCallback = Callable[[Mapping[str, Any]], Awaitable[None]]


class CredentialProvider(Protocol):
    """Acquire Google access tokens using one credential method."""

    name: str
    credential_fields: frozenset[str]

    @property
    def available(self) -> bool:
        """Return whether this provider has the credentials it requires."""

    async def async_get_access_token(self) -> GoogleAuthResponse:
        """Return a Google access token."""


class CookieCredentialProvider:
    """Acquire Google access tokens from an issue token and cookies."""

    name = "cookies"
    credential_fields = frozenset({CONF_ISSUE_TOKEN, CONF_COOKIES})

    def __init__(
        self,
        client: NestClient,
        async_save_credentials: CredentialUpdateCallback,
    ) -> None:
        """Initialize the cookie provider."""
        self._client = client
        self._async_save_credentials = async_save_credentials

    @property
    def available(self) -> bool:
        """Return whether cookie credentials are available."""
        return bool(self._client.issue_token and self._client.cookies)

    async def async_get_access_token(self) -> GoogleAuthResponse:
        """Acquire a token and durably report response-side cookie rotation."""
        issue_token = self._client.issue_token
        cookies = self._client.cookies
        if not issue_token or not cookies:
            raise ValueError("Cookie credentials are not available")

        try:
            return await self._client.get_access_token_from_cookies(
                issue_token, cookies
            )
        finally:
            refreshed = self._client.refreshed_cookies
            if refreshed and refreshed != cookies:
                await self._async_save_credentials({CONF_COOKIES: refreshed})


class RefreshTokenCredentialProvider:
    """Acquire Google access tokens from a legacy refresh token."""

    name = "refresh_token"
    credential_fields = frozenset({CONF_REFRESH_TOKEN})

    def __init__(
        self,
        client: NestClient,
        async_save_credentials: CredentialUpdateCallback,
    ) -> None:
        """Initialize the refresh-token provider."""
        self._client = client
        self._async_save_credentials = async_save_credentials

    @property
    def available(self) -> bool:
        """Return whether a refresh token is available."""
        return bool(self._client.refresh_token)

    async def async_get_access_token(self) -> GoogleAuthResponse:
        """Acquire an access token from a legacy refresh token."""
        refresh_token = self._client.refresh_token
        if not refresh_token:
            raise ValueError("Refresh-token credentials are not available")

        return await self._client.get_access_token_from_refresh_token(refresh_token)


def create_credential_providers(
    client: NestClient,
    async_save_credentials: CredentialUpdateCallback,
) -> Sequence[CredentialProvider]:
    """Create providers in stable fallback order."""
    return (
        CookieCredentialProvider(client, async_save_credentials),
        RefreshTokenCredentialProvider(client, async_save_credentials),
    )


def credential_field_names(
    providers: Sequence[CredentialProvider],
) -> frozenset[str]:
    """Return every config field owned by the registered providers."""
    return frozenset(
        field for provider in providers for field in provider.credential_fields
    )


def apply_credential_updates(
    client: NestClient,
    updates: Mapping[str, Any],
    credential_fields: Collection[str],
) -> None:
    """Apply durable credential fields to the live client."""
    for field in credential_fields:
        if field in updates:
            setattr(client, field, updates[field])
