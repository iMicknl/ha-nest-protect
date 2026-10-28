"""PyNest API Client."""

from __future__ import annotations

import logging
import time
from collections.abc import Collection
from http.cookiejar import http2time
from http.cookies import Morsel
from random import randint
from types import TracebackType
from typing import Any, cast

from aiohttp import (
    ClientResponse,
    ClientSession,
    ClientTimeout,
    ContentTypeError,
    FormData,
)

from .const import (
    APP_LAUNCH_URL_FORMAT,
    DEFAULT_NEST_ENVIRONMENT,
    NEST_AUTH_URL_JWT,
    NEST_REQUEST,
    REQUEST_TIMEOUT_SECONDS,
    SUBSCRIBE_CONNECT_TIMEOUT,
    SUBSCRIBE_TIMEOUT_SECONDS,
    TOKEN_URL,
    USER_AGENT,
)
from .exceptions import (
    BadCredentialsException,
    BadGatewayException,
    EmptyResponseException,
    GatewayTimeoutException,
    GoogleThrottledException,
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
    NestUrls,
)

_LOGGER = logging.getLogger(__package__)

TRANSIENT_CLIENT_STATUSES = {408, 429}

REQUEST_TIMEOUT = ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)


def raise_for_transient_status(response: ClientResponse, action: str) -> None:
    """Raise a NestServiceException for statuses that are worth retrying.

    Checked before the body is parsed: outages often return HTML or an error
    body that would otherwise be mistaken for an auth failure.
    """
    status = response.status

    if status == 502:
        raise BadGatewayException(f"{status} error while {action}")
    if status == 504:
        raise GatewayTimeoutException(f"{status} error while {action}")
    if status >= 500 or status in TRANSIENT_CLIENT_STATUSES:
        raise NestServiceException(f"{status} error while {action}")


def raise_for_google_throttling(response: ClientResponse, action: str) -> None:
    """Raise GoogleThrottledException, carrying Retry-After, on a 429."""
    if response.status == 429:
        raise GoogleThrottledException(
            f"429 error while {action}",
            retry_after=parse_retry_after(response.headers.get("Retry-After")),
        )


def is_cookie_expired(morsel: Morsel[str]) -> bool:
    """Return True if a Set-Cookie morsel tells the client to delete the cookie."""
    # Max-Age takes precedence over Expires (RFC 6265 section 5.3).
    if max_age := morsel["max-age"]:
        try:
            return int(max_age) <= 0
        except ValueError:
            pass

    if expires := morsel["expires"]:
        expires_at = http2time(expires)
        return expires_at is not None and expires_at <= time.time()

    return False


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (seconds or HTTP date) into seconds."""
    if not value:
        return None
    if value.strip().isdigit():
        return float(value.strip())
    retry_at = http2time(value)
    if retry_at is None:
        return None
    return max(retry_at - time.time(), 0.0)


def merge_cookies(
    original: str, new_cookies: dict[str, str], expired: Collection[str] = ()
) -> str:
    """Merge new cookie values into an existing cookie header string.

    New values override existing cookies with the same name, and cookies named
    in expired are removed. Preserves all other cookies.
    We don't use aiohttp's cookie jar because the user-provided cookie string
    spans multiple Google domains/paths that the jar's scoping would break.
    """
    if not new_cookies and not expired:
        return original

    parsed: dict[str, str] = {}
    for raw_part in original.split(";"):
        cookie_part = raw_part.strip()
        if "=" in cookie_part:
            name, value = cookie_part.split("=", 1)
            parsed[name.strip()] = value.strip()

    parsed.update(new_cookies)
    for name in expired:
        parsed.pop(name, None)

    return "; ".join(f"{k}={v}" for k, v in parsed.items())


class NestClient:
    """Interface class for the Nest API."""

    nest_session: NestResponse | None = None
    auth: GoogleAuthResponseForCookies | None = None
    session: ClientSession
    transport_url: str | None = None
    environment: NestEnvironment

    # Legacy Auth
    refresh_token: str | None = None
    # Cookie Auth
    cookies: str | None = None
    issue_token: str | None = None
    # Set after successful cookie auth if Google returned refreshed cookies.
    # Only Google OAuth cookies matter for re-auth; Nest uses Bearer tokens.
    refreshed_cookies: str | None = None

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

    def _drop_expired_cookies(self, cookies: str, expired: Collection[str]) -> None:
        """Remove the cookies Google expired, once its response proved usable."""
        merged = merge_cookies(self.refreshed_cookies or cookies, {}, expired)
        # Keep the stored cookies rather than replacing them with an empty
        # header that later refreshes would silently skip.
        if not merged:
            raise BadCredentialsException("Google expired all session cookies")
        self.refreshed_cookies = merged
        self.cookies = merged

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

        # Same order as setup: entries re-authenticated with cookies may still
        # hold a revoked legacy refresh token.
        if self.issue_token and self.cookies:
            await self.get_access_token_from_cookies(self.issue_token, self.cookies)
        elif self.refresh_token:
            await self.get_access_token_from_refresh_token(self.refresh_token)

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
            timeout=REQUEST_TIMEOUT,
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
            raise_for_transient_status(response, "refreshing Google access token")
            result = await response.json()

            if "error" in result:
                if result["error"] == "invalid_grant":
                    raise BadCredentialsException(result["error"])

                raise Exception(result["error"])

            self.auth = GoogleAuthResponse(**result)

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

        async with self.session.get(
            issue_token,
            timeout=REQUEST_TIMEOUT,
            headers={
                "Sec-Fetch-Mode": "cors",
                "User-Agent": USER_AGENT,
                "X-Requested-With": "XmlHttpRequest",
                "Referer": "https://accounts.google.com/o/oauth2/iframe",
                "cookie": cookies,
            },
        ) as response:
            # Capture refreshed cookies from Google's response
            new_cookies: dict[str, str] = {}
            expired_cookies: set[str] = set()
            for cookie in response.cookies.values():
                if is_cookie_expired(cookie):
                    expired_cookies.add(cookie.key)
                else:
                    new_cookies[cookie.key] = cookie.value

            # Rotated values apply at once: the superseded set stops working
            # even if this request then fails. Deletions wait for a successful
            # response, so an error or throttled answer can't tear down
            # cookies that still work.
            if new_cookies:
                self.refreshed_cookies = merge_cookies(cookies, new_cookies)
                self.cookies = self.refreshed_cookies

            raise_for_google_throttling(response, "retrieving Google access token")
            raise_for_transient_status(response, "retrieving Google access token")

            try:
                result = await response.json()
            except ContentTypeError as exception:
                # Google answers with an HTML page instead of JSON when it is
                # rate limiting or showing an "unusual traffic" interstitial.
                raise GoogleThrottledException(
                    f"{response.status} non-JSON Google access token response"
                ) from exception
            except ValueError as exception:
                raise PynestException(
                    f"{response.status} unexpected Google access token response"
                ) from exception

            if not isinstance(result, dict):
                raise PynestException(
                    f"{response.status} unexpected Google access token response"
                )

            # Any remaining Google error (USER_LOGGED_OUT, consent_required, ...)
            # means the cookies or issue_token can no longer be used.
            if "error" in result:
                detail = result.get("detail")
                raise BadCredentialsException(
                    f"{result['error']} - {detail}" if detail else result["error"]
                )

            try:
                auth = GoogleAuthResponseForCookies(**result)
            except TypeError as exception:
                raise PynestException(
                    f"Unexpected Google access token response fields: {list(result)}"
                ) from exception

            if expired_cookies:
                self._drop_expired_cookies(cookies, expired_cookies)

            self.auth = auth
            return self.auth

    async def authenticate(self, access_token: str) -> NestResponse:
        """Start a new Nest session with an access token."""

        async with self.session.post(
            NEST_AUTH_URL_JWT,
            timeout=REQUEST_TIMEOUT,
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
            raise_for_transient_status(response, "requesting Nest JWT")
            result = await response.json()
            try:
                nest_auth = NestAuthResponse(**result)
            except TypeError as exception:
                raise PynestException(
                    f"{response.status} unexpected Nest JWT response"
                ) from exception

        async with self.session.get(
            self.environment.host + "/session",
            timeout=REQUEST_TIMEOUT,
            headers={
                "Authorization": f"Basic {nest_auth.jwt}",
                "cookie": "G_ENABLED_IDPS=google; eu_cookie_accepted=1; viewer-volume=0.5; cztoken="
                + (nest_auth.jwt or ""),
            },
        ) as response:
            raise_for_transient_status(response, "authenticating")
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

            # The transport host can change between sessions.
            urls = self.nest_session.urls
            if isinstance(urls, NestUrls):
                urls = vars(urls)
            if urls and urls.get("transport_url"):
                self.transport_url = urls["transport_url"]

            return self.nest_session

    async def get_first_data(
        self, nest_access_token: str, user_id: str, request: dict = NEST_REQUEST
    ) -> FirstDataAPIResponse:
        """Get first data."""
        async with self.session.post(
            APP_LAUNCH_URL_FORMAT.format(host=self.environment.host, user_id=user_id),
            timeout=REQUEST_TIMEOUT,
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
            timeout=ClientTimeout(
                total=SUBSCRIBE_TIMEOUT_SECONDS, connect=SUBSCRIBE_CONNECT_TIMEOUT
            ),
            json={
                "objects": objects,
                # "timeout": SUBSCRIBE_TIMEOUT_SECONDS,
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
            timeout=REQUEST_TIMEOUT,
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
