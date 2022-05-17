"""PyNest API Client."""
from __future__ import annotations

import logging
from random import randint
import time
from types import TracebackType
from typing import Any
import urllib.parse

from aiohttp import ClientSession, ClientTimeout, ContentTypeError, FormData

from .const import (
    APP_LAUNCH_URL_FORMAT,
    DEFAULT_NEST_ENVIRONMENT,
    NEST_AUTH_URL_JWT,
    NEST_REQUEST,
    TOKEN_URL,
    USER_AGENT,
)
from .exceptions import (
    BadCredentialsException,
    BadGatewayException,
    GatewayTimeoutException,
    NotAuthenticatedException,
    PynestException,
)
from .models import GoogleAuthResponse, NestAuthResponse, NestEnvironment, NestResponse

_LOGGER = logging.getLogger(__package__)


class NestClient:
    """Interface class for the Nest API."""

    nest_session: NestResponse | None = None
    auth: GoogleAuthResponse | None = None
    session: ClientSession
    transport_url: str | None = None
    environment: NestEnvironment

    def __init__(
        self,
        session: ClientSession | None = None,
        refresh_token: str | None = None,
        environment: NestEnvironment = DEFAULT_NEST_ENVIRONMENT,
    ) -> None:
        """Initialize NestClient."""

        self.session = session if session else ClientSession()
        self.refresh_token = refresh_token
        self.environment = environment

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
    def generate_token_url(
        environment: NestEnvironment = DEFAULT_NEST_ENVIRONMENT,
    ) -> str:
        """Generate the URL to get a Nest authentication token."""
        data = {
            "access_type": "offline",
            "response_type": "code",
            "scope": "openid profile email https://www.googleapis.com/auth/nest-account",
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            "client_id": environment.client_id,
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
                    "client_id": self.environment.client_id,
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
                if result["error"] == "invalid_grant":
                    raise BadCredentialsException(result["error"])

                raise Exception(result["error"])

            refresh_token = result["refresh_token"]
            self.refresh_token = refresh_token

            return refresh_token

    async def get_access_token(
        self, refresh_token: str | None = None
    ) -> GoogleAuthResponse:
        """Get a Nest refresh token from an authorization code."""

        if refresh_token:
            self.refresh_token = refresh_token

        if not self.refresh_token:
            raise Exception("No refresh token")

        async with self.session.post(
            TOKEN_URL,
            data=FormData(
                {
                    "refresh_token": self.refresh_token,
                    "client_id": self.environment.client_id,
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
                if result["error"] == "invalid_grant":
                    raise BadCredentialsException(result["error"])

                raise Exception(result["error"])

            self.auth = GoogleAuthResponse(**result)

            return self.auth

    async def authenticate(self, access_token: str) -> NestResponse:
        """Start a new Nest session with an access token."""
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
                "Referer": self.environment.host,
            },
        ) as response:
            result = await response.json()
            nest_auth = NestAuthResponse(**result)

        async with self.session.get(
            self.environment.host + "/session",
            headers={
                "Authorization": f"Basic {nest_auth.jwt}",
                "cookie": "G_ENABLED_IDPS=google; eu_cookie_accepted=1; viewer-volume=0.5; cztoken="
                + nest_auth.jwt,
            },
        ) as response:
            try:
                nest_response = await response.json()
            except ContentTypeError:
                nest_response = await response.text()

                raise PynestException(
                    f"{response.status} error while authenticating - {nest_response}. Please create an issue on GitHub."
                )

            # Change variable names since Python cannot handle vars that start with a number
            if nest_response.get("2fa_state"):
                nest_response["_2fa_state"] = nest_response.pop("2fa_state")
            if nest_response.get("2fa_enabled"):
                nest_response["_2fa_enabled"] = nest_response.pop("2fa_enabled")
            if nest_response.get("2fa_state_changed"):
                nest_response["_2fa_state_changed"] = nest_response.pop(
                    "2fa_state_changed"
                )

            try:
                self.nest_session = NestResponse(**nest_response)
            except Exception:
                nest_response = await response.text()

                raise PynestException(
                    f"{response.status} error while authenticating - {nest_response}. Please create an issue on GitHub."
                )

            return self.nest_session

    async def get_first_data(self, nest_access_token: str, user_id: str) -> Any:
        """Get a Nest refresh token from an authorization code."""
        async with self.session.post(
            APP_LAUNCH_URL_FORMAT.format(host=self.environment.host, user_id=user_id),
            json=NEST_REQUEST,
            headers={
                "Authorization": f"Basic {nest_access_token}",
                "X-nl-user-id": user_id,
                "X-nl-protocol-version": str(1),
            },
        ) as response:
            result = await response.json()

            if result.get("error"):
                _LOGGER.debug(result)

            self.transport_url = result["service_urls"]["urls"]["transport_url"]

            return result

    async def subscribe_for_data(
        self,
        nest_access_token: str,
        user_id: str,
        transport_url: str,
        updated_buckets: dict,
    ) -> Any:
        """Subscribe for data."""

        epoch = int(time.time())
        random = str(randint(100, 999))
        timeout = 3600 * 24

        # TODO throw better exceptions
        async with self.session.post(
            f"{transport_url}/v6/subscribe",
            timeout=ClientTimeout(total=timeout),
            json={
                "objects": updated_buckets,
                "timeout": timeout,
                "sessionID": f"ios-${user_id}.{random}.{epoch}",
            },
            headers={
                "Authorization": f"Basic {nest_access_token}",
                "X-nl-user-id": user_id,
                "X-nl-protocol-version": str(1),
            },
        ) as response:
            if response.status == 401:
                raise NotAuthenticatedException(await response.text())

            if response.status == 504:
                raise GatewayTimeoutException(await response.text())

            if response.status == 502:
                raise BadGatewayException(await response.text())

            try:
                result = await response.json()
            except ContentTypeError:
                result = await response.text()

                raise PynestException(
                    f"{response.status} error while subscribing - {result}"
                )

            # TODO type object

            return result

    async def update_objects(
        self,
        nest_access_token: str,
        user_id: str,
        transport_url: str,
        objects_to_update: dict,
    ) -> Any:
        """Subscribe for data."""

        epoch = int(time.time())
        random = str(randint(100, 999))

        # TODO throw better exceptions
        async with self.session.post(
            f"{transport_url}/v6/put",
            json={
                "session": f"ios-${user_id}.{random}.{epoch}",
                "objects": objects_to_update,
            },
            headers={
                "Authorization": f"Basic {nest_access_token}",
                "X-nl-user-id": user_id,
                "X-nl-protocol-version": str(1),
            },
        ) as response:
            if response.status == 401:
                raise NotAuthenticatedException(await response.text())

            try:
                result = await response.json()
            except ContentTypeError:
                result = await response.text()

                raise PynestException(
                    f"{response.status} error while subscribing - {result}"
                )

            # TODO type object

            return result


# https://czfe82-front01-iad01.transport.home.nest.com/v5/put
# {
# 	"session": "30523153.35436.1646600092822",
# 	"objects": [{
# 		"base_object_revision": 25277,
# 		"object_key": "topaz.18B43000418C356F",
# 		"op": "MERGE",
# 		"value": {
# 			"night_light_enable": true,
# 			"night_light_continuous": true
# 		}
# 	}]
# }
