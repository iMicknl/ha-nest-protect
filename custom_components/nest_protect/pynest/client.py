"""PyNest API Client."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from random import randint
from types import TracebackType
from typing import Any, cast

from aiohttp import ClientSession, ClientTimeout, ContentTypeError, FormData

from .const import (
    APP_LAUNCH_URL_FORMAT,
    DEFAULT_NEST_ENVIRONMENT,
    GOOGLE_HOME_APP,
    GOOGLE_OAUTH_CLIENT_SIG,
    NEST_ACCOUNT_OAUTH_SERVICE,
    NEST_AUTH_URL_JWT,
    NEST_REQUEST,
    TOKEN_URL,
    USER_AGENT,
)
from .exceptions import (
    BadCredentialsException,
    BadGatewayException,
    EmptyResponseException,
    GatewayTimeoutException,
    NestServiceException,
    NotAuthenticatedException,
    PynestException,
)
from .models import (
    Bucket,
    FirstDataAPIResponse,
    GoogleAuthResponse,
    GoogleAuthResponseForCookies,
    NestAuthResponse,
    NestEnvironment,
    NestResponse,
)

_LOGGER = logging.getLogger(__package__)


def merge_cookies(original: str, new_cookies: dict[str, str]) -> str:
    """Merge new cookie values into an existing cookie header string.

    New values override existing cookies with the same name.
    Preserves cookies not present in new_cookies.
    We don't use aiohttp's cookie jar because the user-provided cookie string
    spans multiple Google domains/paths that the jar's scoping would break.
    """
    if not new_cookies:
        return original

    parsed: dict[str, str] = {}
    for raw_part in original.split(";"):
        cookie_part = raw_part.strip()
        if "=" in cookie_part:
            name, value = cookie_part.split("=", 1)
            parsed[name.strip()] = value.strip()

    parsed.update(new_cookies)

    return "; ".join(f"{k}={v}" for k, v in parsed.items())


class NestClient:
    """Interface class for the Nest API."""

    nest_session: NestResponse | None = None
    auth: GoogleAuthResponseForCookies | None = None
    session: ClientSession
    transport_url: str | None = None
    environment: NestEnvironment

    # Master token Auth (durable, app-style)
    master_token: str | None = None
    google_email: str | None = None
    android_id: str | None = None
    # Legacy Auth
    refresh_token: str | None = None
    # Cookie Auth
    cookies: str | None = None
    issue_token: str | None = None
    # Set after successful cookie auth if Google returned refreshed cookies.
    # Only Google OAuth cookies matter for re-auth; Nest uses Bearer tokens.
    refreshed_cookies: str | None = None
    refreshed_issue_token: str | None = None
    refreshed_refresh_token: str | None = None
    last_issue_token_response_keys: set[str] | None = None

    def __init__(
        self,
        session: ClientSession | None = None,
        # refresh_token: str | None = None,
        # issue_token: str | None = None,
        # cookies: str | None = None,
        environment: NestEnvironment = DEFAULT_NEST_ENVIRONMENT,
    ) -> None:
        """Initialize NestClient."""

        self.session = session or ClientSession()
        # self.refresh_token = refresh_token
        # self.issue_token = issue_token
        # self.cookies = cookies
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

    async def get_access_token(self) -> GoogleAuthResponse:
        """Get a Nest access token."""

        if self.master_token:
            await self.get_access_token_from_master_token()
        elif self.refresh_token:
            await self.get_access_token_from_refresh_token(self.refresh_token)
        elif self.issue_token and self.cookies:
            await self.get_access_token_from_cookies(self.issue_token, self.cookies)

        return self.auth

    async def get_access_token_from_refresh_token(
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

    @staticmethod
    def generate_android_id() -> str:
        """Generate a random 16-hex-character Android ID for the master-token flow."""
        return secrets.token_hex(8)

    @staticmethod
    def _exchange_token_sync(oauth_token: str, email: str, android_id: str) -> dict:
        """Blocking gpsoauth call: one-time oauth_token -> durable master token."""
        import gpsoauth  # noqa: PLC0415  (optional dependency, imported lazily)

        return gpsoauth.exchange_token(email, oauth_token, android_id)

    @staticmethod
    def _perform_oauth_sync(email: str, master_token: str, android_id: str) -> dict:
        """Blocking gpsoauth call: master token -> short-lived nest-account token."""
        import gpsoauth  # noqa: PLC0415  (optional dependency, imported lazily)

        return gpsoauth.perform_oauth(
            email,
            master_token,
            android_id,
            app=GOOGLE_HOME_APP,
            service=NEST_ACCOUNT_OAUTH_SERVICE,
            client_sig=GOOGLE_OAUTH_CLIENT_SIG,
        )

    async def exchange_master_token(
        self, oauth_token: str, email: str, android_id: str
    ) -> str:
        """Exchange a one-time ``oauth_token`` for a durable Google master token.

        The ``oauth_token`` (starts with ``oauth2_4/``) is captured once from
        ``accounts.google.com/EmbeddedSetup``. The resulting master token
        (``aas_et/...``) never expires unless the password is changed or access is
        revoked, mirroring how the mobile apps stay signed in.
        """
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, self._exchange_token_sync, oauth_token, email, android_id
        )

        master_token = result.get("Token")
        if not master_token:
            error = result.get("Error", result)
            raise BadCredentialsException(f"Could not obtain master token: {error}")

        self.master_token = master_token
        self.google_email = email
        self.android_id = android_id

        return master_token

    async def get_access_token_from_master_token(
        self,
        master_token: str | None = None,
        email: str | None = None,
        android_id: str | None = None,
    ) -> GoogleAuthResponse:
        """Mint a short-lived nest-account access token from a master token."""
        if master_token:
            self.master_token = master_token
        if email:
            self.google_email = email
        if android_id:
            self.android_id = android_id

        if not (self.master_token and self.google_email and self.android_id):
            raise PynestException("Master token credentials are incomplete")

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            self._perform_oauth_sync,
            self.google_email,
            self.master_token,
            self.android_id,
        )

        access_token = result.get("Auth")
        if not access_token:
            error = result.get("Error", result)
            # BadAuthentication means the master token is no longer valid.
            raise BadCredentialsException(
                f"Could not mint access token from master token: {error}"
            )

        expiry = int(result.get("Expiry", 0))
        expires_in = max(0, expiry - int(time.time())) if expiry else 3600

        self.auth = GoogleAuthResponse(
            access_token=access_token,
            scope=NEST_ACCOUNT_OAUTH_SERVICE,
            token_type="Bearer",
            expires_in=expires_in,
            id_token=None,
        )

        return self.auth

    async def get_access_token_from_cookies(
        self, issue_token: str, cookies: str
    ) -> GoogleAuthResponse:
        """Get a Nest refresh token from an issue token and cookies."""

        if issue_token:
            self.issue_token = issue_token

        if cookies:
            self.cookies = cookies

        self.refreshed_cookies = None
        self.refreshed_issue_token = None
        self.refreshed_refresh_token = None

        async with self.session.get(
            issue_token,
            headers={
                "Sec-Fetch-Mode": "cors",
                "User-Agent": USER_AGENT,
                "X-Requested-With": "XmlHttpRequest",
                "Referer": "https://accounts.google.com/o/oauth2/iframe",
                "cookie": cookies,
            },
        ) as response:
            if (
                "action=issueToken" in str(response.url)
                and str(response.url) != self.issue_token
            ):
                self.refreshed_issue_token = str(response.url)

            # Capture refreshed cookies from Google's response
            new_cookies: dict[str, str] = {}
            for cookie in response.cookies.values():
                new_cookies[cookie.key] = cookie.value

            if new_cookies:
                self.refreshed_cookies = merge_cookies(cookies, new_cookies)

            result = await response.json()
            self.last_issue_token_response_keys = set(result.keys())

            if "error" in result:
                self.refreshed_cookies = None
                self.refreshed_issue_token = None
                # Cookie method
                if result["error"] == "USER_LOGGED_OUT":
                    raise BadCredentialsException(
                        f"{result['error']} - {result['detail']}"
                    )

                raise Exception(result["error"])

            if refresh_token := result.get("refresh_token"):
                self.refreshed_refresh_token = refresh_token
                _LOGGER.debug("issueToken returned a refresh_token")

            self.auth = GoogleAuthResponseForCookies(**result)

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
                + (nest_auth.jwt or ""),
            },
        ) as response:
            try:
                nest_response = await response.json()
            except ContentTypeError as exception:
                nest_response = await response.text()

                raise PynestException(
                    f"{response.status} error while authenticating - {nest_response}. Please create an issue on GitHub."
                ) from exception

            # Change variable names since Python cannot handle vars that start with a number
            if nest_response.get("2fa_state"):
                nest_response["_2fa_state"] = nest_response.pop("2fa_state")
            if nest_response.get("2fa_enabled"):
                nest_response["_2fa_enabled"] = nest_response.pop("2fa_enabled")
            if nest_response.get("2fa_state_changed"):
                nest_response["_2fa_state_changed"] = nest_response.pop(
                    "2fa_state_changed"
                )

            if nest_response.get("error"):
                _LOGGER.error("Authentication error: %s", nest_response.get("error"))

                raise PynestException(
                    f"{response.status} error while authenticating - {nest_response}."
                )

            try:
                self.nest_session = NestResponse(**nest_response)
            except Exception as exception:
                nest_response = await response.text()

                if result.get("error"):
                    _LOGGER.exception("Could not interpret Nest response")

                raise PynestException(
                    f"{response.status} error while authenticating - {nest_response}. Please create an issue on GitHub."
                ) from exception

            return self.nest_session

    async def get_first_data(
        self, nest_access_token: str, user_id: str, request: dict = NEST_REQUEST
    ) -> FirstDataAPIResponse:
        """Get first data."""
        async with self.session.post(
            APP_LAUNCH_URL_FORMAT.format(host=self.environment.host, user_id=user_id),
            json=request,
            headers={
                "Authorization": f"Basic {nest_access_token}",
                "X-nl-user-id": user_id,
                "X-nl-protocol-version": str(1),
            },
        ) as response:
            result = await response.json()

            if "2fa_enabled" in result:
                result["_2fa_enabled"] = result.pop("2fa_enabled")

            if result.get("error"):
                _LOGGER.debug(
                    "Received error from Nest service: %s", await response.text()
                )

                raise PynestException(
                    f"{response.status} error while subscribing - {result}"
                )

            result = FirstDataAPIResponse(**result)

            self.transport_url = result.service_urls["urls"]["transport_url"]

            return result

    async def subscribe_for_data(
        self,
        nest_access_token: str,
        user_id: str,
        transport_url: str,
        updated_buckets: dict,
    ) -> Any:
        """Subscribe for data."""
        timeout = 600

        objects = []
        for bucket in updated_buckets:
            bucket = cast(Bucket, bucket)
            objects.append(
                {
                    "object_key": bucket.object_key,
                    "object_revision": bucket.object_revision,
                    "object_timestamp": bucket.object_timestamp,
                }
            )

        # TODO throw better exceptions
        async with self.session.post(
            f"{transport_url}/v6/subscribe",
            timeout=ClientTimeout(total=timeout),
            json={
                "objects": objects,
                # "timeout": timeout,
                # "sessionID": f"ios-${user_id}.{random}.{epoch}",
            },
            headers={
                "Authorization": f"Basic {nest_access_token}",
                "X-nl-user-id": user_id,
                "X-nl-protocol-version": str(1),
            },
        ) as response:
            _LOGGER.debug("Data received via subscriber (status: %s)", response.status)

            if response.status == 401:
                raise NotAuthenticatedException(await response.text())

            if response.status == 504:
                raise GatewayTimeoutException(await response.text())

            if response.status == 502:
                raise BadGatewayException(await response.text())

            if response.status == 200 and response.content_type == "text/plain":
                raise EmptyResponseException(await response.text())

            try:
                result = await response.json()
            except ContentTypeError as error:
                result = await response.text()

                raise NestServiceException(
                    f"{response.status} error while subscribing - {result}"
                ) from error

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
            except ContentTypeError as err:
                result = await response.text()

                raise PynestException(
                    f"{response.status} error while subscribing - {result}"
                ) from err

            # TODO type object

            return result
