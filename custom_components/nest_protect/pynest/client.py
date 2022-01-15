"""PyNest API Client."""
from __future__ import annotations

from types import TracebackType
from typing import Any
import urllib.parse

from aiohttp import ClientSession, FormData

from .const import CLIENT_ID, NEST_AUTH_URL_JWT, NEST_REQUEST, TOKEN_URL, USER_AGENT
from .models import NestResponse


class NestClient:
    """Interface class for the Nest API."""

    session: ClientSession

    def __init__(
        self,
        session: ClientSession | None = None,
    ) -> None:
        """Initialize NestClient."""

        self.session = session if session else ClientSession()

    async def __aenter__(self) -> NestClient:
        """__aenter__."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """__aexit__."""
        await self.session.close()

    @staticmethod
    def generate_token_url() -> str:
        """Generate the URL to get a Nest authentication token."""
        data = {
            "access_type": "offline",
            "response_type": "code",
            "scope": "openid profile email https://www.googleapis.com/auth/nest-account",
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            "client_id": CLIENT_ID,
        }

        return f"https://accounts.google.com/o/oauth2/auth/oauthchooseaccount?{urllib.parse.urlencode(data)}"

    async def get_refresh_token(self, token: str) -> Any:
        """Get a Nest refresh token from an authorization code."""
        async with self.session.post(
            TOKEN_URL,
            data=FormData(
                {
                    "code": token,
                    "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                    "client_id": CLIENT_ID,
                    "grant_type": "authorization_code",
                }
            ),
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ) as response:
            result = await response.json()

            if "error" in result:
                raise Exception(result["error"])

            refresh_token = result["refresh_token"]

            return refresh_token

    async def get_access_token(self, refresh_token: str) -> Any:
        """Get a Nest refresh token from an authorization code."""
        async with self.session.post(
            TOKEN_URL,
            data=FormData(
                {
                    "refresh_token": refresh_token,
                    "client_id": CLIENT_ID,
                    "grant_type": "refresh_token",
                }
            ),
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ) as response:
            result = await response.json()

            if "error" in result:
                raise Exception(result["error"])

            access_token = result["access_token"]

            return access_token

    async def authenticate(self, access_token: str) -> NestResponse:
        """Get a Nest refresh token from an authorization code."""
        async with self.session.post(
            NEST_AUTH_URL_JWT,
            data=FormData(
                {
                    "embed_google_oauth_access_token": True,
                    "expire_after": "3600s",
                    "google_oauth_access_token": access_token,
                    "policy_id": "authproxy-oauth-policy",
                }
            ),
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": USER_AGENT,
                "Referer": "https://home.nest.com",
            },
        ) as response:
            result = await response.json()

            jwt = result["jwt"]

        async with self.session.get(
            "https://home.nest.com/session",
            headers={
                "Authorization": f"Basic {jwt}",
                "cookie": "G_ENABLED_IDPS=google; eu_cookie_accepted=1; viewer-volume=0.5; cztoken="
                + jwt,
            },
        ) as response:
            nest_response = await response.json()

            # Change variable names since Python cannot handle vars that start with a number
            nest_response["_2fa_state"] = nest_response.pop("2fa_state")
            nest_response["_2fa_enabled"] = nest_response.pop("2fa_enabled")
            nest_response["_2fa_state_changed"] = nest_response.pop("2fa_state_changed")

            return NestResponse(**nest_response)

    async def get_first_data(self, nest_access_token: str, user_id: str) -> Any:
        """Get a Nest refresh token from an authorization code."""
        async with self.session.post(
            f"https://home.nest.com/api/0.1/user/{user_id}/app_launch",
            json=NEST_REQUEST,
            headers={
                "Authorization": f"Basic {nest_access_token}",
                "X-nl-user-id": user_id,
                "X-nl-protocol-version": str(1),
            },
        ) as response:
            result = await response.json()

            return result

    async def get_data(
        self, nest_access_token: str, user_id: str, transport_url: str
    ) -> Any:
        """Get a Nest refresh token from an authorization code."""
        async with self.session.post(
            f"{transport_url}/v5/subscribe",
            json=NEST_REQUEST,
            headers={
                "Authorization": f"Basic {nest_access_token}",
                "X-nl-user-id": user_id,
                "X-nl-protocol-version": str(1),
            },
        ) as response:
            result = await response.text()

            return result
