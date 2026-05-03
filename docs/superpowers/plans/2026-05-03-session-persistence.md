# Session Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist Nest session across HA restarts so the integration can skip Google re-authentication when the session is still valid, and refresh cookies on successful auth to extend their lifetime.

**Architecture:** Three-tier fallback on startup (persisted session -> cookie auth -> auth failed). Cookie refresh captures Google's Set-Cookie response headers. Store-based persistence via HA's `homeassistant.helpers.storage.Store`. Exponential backoff in subscription loop.

**Tech Stack:** Python 3.14, Home Assistant Core 2026.4+, aiohttp, pytest with pytest-homeassistant-custom-component

---

## File Structure

| File | Responsibility |
|------|---------------|
| `custom_components/nest_protect/const.py` | Add storage/session constants |
| `custom_components/nest_protect/pynest/models.py` | Add serialization to `NestResponse` |
| `custom_components/nest_protect/pynest/client.py` | Return refreshed cookies from Google auth, add cookie merge helper |
| `custom_components/nest_protect/__init__.py` | Three-tier startup, Store load/save, cookie update, backoff |
| `tests/pynest/test_client.py` | Tests for cookie refresh extraction and merge |
| `tests/test_init.py` | Tests for three-tier startup and session persistence |

---

### Task 1: Add Constants

**Files:**
- Modify: `custom_components/nest_protect/const.py`

- [ ] **Step 1: Add storage and session constants**

```python
# Add to const.py after existing constants:

STORAGE_VERSION: Final = 1
STORAGE_KEY_FORMAT: Final = "nest_protect_{entry_id}"
SESSION_EXPIRY_BUFFER_SECONDS: Final = 300  # 5 minutes
MAX_AUTH_FAILURES: Final = 3
BACKOFF_INTERVALS: Final = (30, 60, 120, 300, 600)  # seconds, capped at 10 min
```

- [ ] **Step 2: Commit**

```bash
git add custom_components/nest_protect/const.py
git commit -m "feat: add constants for session persistence and backoff"
```

---

### Task 2: Add Serialization to NestResponse

**Files:**
- Modify: `custom_components/nest_protect/pynest/models.py`
- Test: `tests/pynest/test_models.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/pynest/test_models.py`:

```python
"""Tests for pynest models."""

import datetime
from unittest.mock import patch

import pytest

from custom_components.nest_protect.pynest.models import NestResponse


@pytest.fixture
def nest_response():
    """Create a sample NestResponse."""
    return NestResponse(
        access_token="test-token",
        email="test@example.com",
        expires_in="Sat, 03-May-2026 20:00:00 GMT",
        userid="user123",
        is_superuser=False,
        language="en",
        weave={"access_token": "weave-token"},
        user="user.123",
        is_staff=False,
    )


def test_nest_response_to_dict(nest_response):
    """Test serialization to dict."""
    result = nest_response.to_dict()
    assert result == {
        "access_token": "test-token",
        "email": "test@example.com",
        "expires_in": "Sat, 03-May-2026 20:00:00 GMT",
        "userid": "user123",
        "is_superuser": False,
        "language": "en",
        "weave": {"access_token": "weave-token"},
        "user": "user.123",
        "is_staff": False,
    }


def test_nest_response_from_dict():
    """Test deserialization from dict."""
    data = {
        "access_token": "test-token",
        "email": "test@example.com",
        "expires_in": "Sat, 03-May-2026 20:00:00 GMT",
        "userid": "user123",
        "is_superuser": False,
        "language": "en",
        "weave": {"access_token": "weave-token"},
        "user": "user.123",
        "is_staff": False,
    }
    result = NestResponse.from_dict(data)
    assert result.access_token == "test-token"
    assert result.email == "test@example.com"
    assert result.userid == "user123"


def test_nest_response_from_dict_returns_none_on_invalid():
    """Test from_dict returns None with missing fields."""
    assert NestResponse.from_dict({}) is None
    assert NestResponse.from_dict(None) is None


def test_nest_response_is_expired_with_buffer():
    """Test that is_expired respects the buffer parameter."""
    # Session expires in 4 minutes - within the 5 minute buffer
    future = datetime.datetime.now() + datetime.timedelta(minutes=4)
    expires_str = future.strftime("%a, %d-%b-%Y %H:%M:%S") + " GMT"

    response = NestResponse(
        access_token="t",
        email="e",
        expires_in=expires_str,
        userid="u",
        is_superuser=False,
        language="en",
        weave={},
        user="u",
        is_staff=False,
    )

    # Without buffer: not expired
    assert response.is_expired() is False
    # With 5 min buffer: expired
    assert response.is_expired(buffer_seconds=300) is True


def test_nest_response_is_expired_without_buffer():
    """Test that is_expired works without buffer (past expiry)."""
    past = datetime.datetime.now() - datetime.timedelta(minutes=10)
    expires_str = past.strftime("%a, %d-%b-%Y %H:%M:%S") + " GMT"

    response = NestResponse(
        access_token="t",
        email="e",
        expires_in=expires_str,
        userid="u",
        is_superuser=False,
        language="en",
        weave={},
        user="u",
        is_staff=False,
    )

    assert response.is_expired() is True
    assert response.is_expired(buffer_seconds=300) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect pytest tests/pynest/test_models.py -v`
Expected: FAIL - `NestResponse` has no `to_dict`, `from_dict`, or `buffer_seconds` param

- [ ] **Step 3: Implement serialization methods and buffer param**

In `custom_components/nest_protect/pynest/models.py`, modify the `NestResponse` class:

```python
@dataclass
class NestResponse:
    """Class that reflects a Nest API response."""

    access_token: float
    email: str
    expires_in: str
    userid: str
    is_superuser: bool
    language: str
    weave: dict[str, str]
    user: str
    is_staff: bool
    error: dict | None = None
    urls: NestUrls = field(default_factory=NestUrls)
    limits: NestLimits = field(default_factory=NestLimits)

    _2fa_state: str = None
    _2fa_enabled: bool = None
    _2fa_state_changed: str = None

    def is_expired(self, buffer_seconds: int = 0):
        """Check if session is expired, with optional early-expiry buffer."""
        expiry_date = datetime.datetime.strptime(
            self.expires_in, "%a, %d-%b-%Y %H:%M:%S %Z"
        )
        return expiry_date <= datetime.datetime.now() + datetime.timedelta(
            seconds=buffer_seconds
        )

    def to_dict(self) -> dict:
        """Serialize session fields needed for persistence."""
        return {
            "access_token": self.access_token,
            "email": self.email,
            "expires_in": self.expires_in,
            "userid": self.userid,
            "is_superuser": self.is_superuser,
            "language": self.language,
            "weave": self.weave,
            "user": self.user,
            "is_staff": self.is_staff,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> NestResponse | None:
        """Deserialize from persisted dict. Returns None if data is invalid."""
        if not data:
            return None
        required = ("access_token", "email", "expires_in", "userid",
                    "is_superuser", "language", "weave", "user", "is_staff")
        if not all(key in data for key in required):
            return None
        return cls(
            access_token=data["access_token"],
            email=data["email"],
            expires_in=data["expires_in"],
            userid=data["userid"],
            is_superuser=data["is_superuser"],
            language=data["language"],
            weave=data["weave"],
            user=data["user"],
            is_staff=data["is_staff"],
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect pytest tests/pynest/test_models.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/nest_protect/pynest/models.py tests/pynest/test_models.py
git commit -m "feat: add serialization and expiry buffer to NestResponse"
```

---

### Task 3: Cookie Refresh in NestClient

**Files:**
- Modify: `custom_components/nest_protect/pynest/client.py`
- Test: `tests/pynest/test_client.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/pynest/test_client.py`:

```python
from custom_components.nest_protect.pynest.client import NestClient, merge_cookies


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect pytest tests/pynest/test_client.py::test_merge_cookies_overrides_existing tests/pynest/test_client.py::test_get_access_token_from_cookies_captures_refreshed_cookies -v`
Expected: FAIL - `merge_cookies` not defined, `refreshed_cookies` not an attribute

- [ ] **Step 3: Implement cookie merge helper and modify get_access_token_from_cookies**

In `custom_components/nest_protect/pynest/client.py`, add the `merge_cookies` function at module level (before the class):

```python
def merge_cookies(original: str, new_cookies: dict[str, str]) -> str:
    """Merge new cookie values into an existing cookie header string.

    New values override existing cookies with the same name.
    Preserves cookies not present in new_cookies.
    """
    if not new_cookies:
        return original

    # Parse original cookies into ordered dict
    parsed: dict[str, str] = {}
    for part in original.split(";"):
        part = part.strip()
        if "=" in part:
            name, value = part.split("=", 1)
            parsed[name.strip()] = value.strip()

    # Override with new values
    parsed.update(new_cookies)

    return "; ".join(f"{k}={v}" for k, v in parsed.items())
```

In the `NestClient` class, add the `refreshed_cookies` attribute and modify `get_access_token_from_cookies`:

```python
class NestClient:
    """Interface class for the Nest API."""

    nest_session: NestResponse | None = None
    auth: GoogleAuthResponseForCookies | None = None
    session: ClientSession
    transport_url: str | None = None
    environment: NestEnvironment
    # Set after successful cookie auth if Google returned refreshed cookies
    refreshed_cookies: str | None = None

    # Legacy Auth
    refresh_token: str | None = None
    # Cookie Auth
    cookies: str | None = None
    issue_token: str | None = None
```

Modify `get_access_token_from_cookies` to capture Set-Cookie headers:

```python
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
            for cookie in response.cookies.values():
                new_cookies[cookie.key] = cookie.value

            if new_cookies:
                self.refreshed_cookies = merge_cookies(cookies, new_cookies)

            result = await response.json()

            if "error" in result:
                # Cookie method
                if result["error"] == "USER_LOGGED_OUT":
                    raise BadCredentialsException(
                        f"{result['error']} - {result['detail']}"
                    )

                raise Exception(result["error"])

            self.auth = GoogleAuthResponseForCookies(**result)

            return self.auth
```

- [ ] **Step 4: Run all client tests to verify they pass**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect pytest tests/pynest/test_client.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/nest_protect/pynest/client.py tests/pynest/test_client.py
git commit -m "feat: capture refreshed cookies from Google OAuth response"
```

---

### Task 4: Three-Tier Startup with Session Persistence

**Files:**
- Modify: `custom_components/nest_protect/__init__.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_init.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_init.py`:

```python
import datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.storage import Store

from custom_components.nest_protect.const import (
    CONF_COOKIES,
    SESSION_EXPIRY_BUFFER_SECONDS,
    STORAGE_KEY_FORMAT,
    STORAGE_VERSION,
)
from custom_components.nest_protect.pynest.models import NestResponse


async def test_startup_reuses_persisted_session(
    hass,
    component_setup_with_cookies,
    config_entry_with_cookies,
):
    """Test that a valid persisted session skips Google re-auth."""
    future = datetime.datetime.now() + datetime.timedelta(minutes=30)
    expires_str = future.strftime("%a, %d-%b-%Y %H:%M:%S") + " GMT"

    stored_data = {
        "nest_session": {
            "access_token": "persisted-token",
            "email": "test@test.com",
            "expires_in": expires_str,
            "userid": "user1",
            "is_superuser": False,
            "language": "en",
            "weave": {},
            "user": "user.1",
            "is_staff": False,
        },
        "transport_url": "https://transport.example.com",
    }

    with (
        patch(
            "custom_components.nest_protect.Store.async_load",
            return_value=stored_data,
        ),
        patch(
            "custom_components.nest_protect.NestClient.get_first_data"
        ) as mock_first_data,
        patch(
            "custom_components.nest_protect.NestClient.get_access_token_from_cookies"
        ) as mock_cookie_auth,
    ):
        mock_first_data.return_value = MagicMock(
            updated_buckets=[], service_urls={"urls": {"transport_url": "https://t.example.com"}}
        )
        await component_setup_with_cookies()

    # Should NOT have called Google cookie auth
    mock_cookie_auth.assert_not_called()
    assert config_entry_with_cookies.state is ConfigEntryState.LOADED


async def test_startup_falls_through_on_expired_session(
    hass,
    component_setup_with_cookies,
    config_entry_with_cookies,
):
    """Test that an expired persisted session triggers cookie re-auth."""
    past = datetime.datetime.now() - datetime.timedelta(minutes=10)
    expires_str = past.strftime("%a, %d-%b-%Y %H:%M:%S") + " GMT"

    stored_data = {
        "nest_session": {
            "access_token": "expired-token",
            "email": "test@test.com",
            "expires_in": expires_str,
            "userid": "user1",
            "is_superuser": False,
            "language": "en",
            "weave": {},
            "user": "user.1",
            "is_staff": False,
        },
        "transport_url": "https://transport.example.com",
    }

    with (
        patch(
            "custom_components.nest_protect.Store.async_load",
            return_value=stored_data,
        ),
        patch(
            "custom_components.nest_protect.NestClient.get_access_token_from_cookies"
        ) as mock_cookie_auth,
        patch("custom_components.nest_protect.NestClient.authenticate") as mock_auth,
        patch("custom_components.nest_protect.NestClient.get_first_data") as mock_first_data,
        patch("custom_components.nest_protect.Store.async_save"),
    ):
        mock_cookie_auth.return_value = MagicMock(access_token="new-google-token")
        mock_auth.return_value = MagicMock(
            access_token="new-nest-token",
            userid="user1",
            is_expired=lambda buffer_seconds=0: False,
        )
        mock_first_data.return_value = MagicMock(
            updated_buckets=[], service_urls={"urls": {"transport_url": "https://t.example.com"}}
        )
        await component_setup_with_cookies()

    # Should have called Google cookie auth as fallback
    mock_cookie_auth.assert_called_once()
    assert config_entry_with_cookies.state is ConfigEntryState.LOADED


async def test_startup_falls_through_on_401_from_persisted_session(
    hass,
    component_setup_with_cookies,
    config_entry_with_cookies,
):
    """Test that a 401 from persisted session triggers cookie re-auth."""
    future = datetime.datetime.now() + datetime.timedelta(minutes=30)
    expires_str = future.strftime("%a, %d-%b-%Y %H:%M:%S") + " GMT"

    stored_data = {
        "nest_session": {
            "access_token": "invalid-token",
            "email": "test@test.com",
            "expires_in": expires_str,
            "userid": "user1",
            "is_superuser": False,
            "language": "en",
            "weave": {},
            "user": "user.1",
            "is_staff": False,
        },
        "transport_url": "https://transport.example.com",
    }

    from custom_components.nest_protect.pynest.exceptions import NotAuthenticatedException

    with (
        patch(
            "custom_components.nest_protect.Store.async_load",
            return_value=stored_data,
        ),
        patch(
            "custom_components.nest_protect.NestClient.get_first_data",
            side_effect=[NotAuthenticatedException("401"), MagicMock(
                updated_buckets=[], service_urls={"urls": {"transport_url": "https://t.example.com"}}
            )],
        ),
        patch(
            "custom_components.nest_protect.NestClient.get_access_token_from_cookies"
        ) as mock_cookie_auth,
        patch("custom_components.nest_protect.NestClient.authenticate") as mock_auth,
        patch("custom_components.nest_protect.Store.async_save"),
    ):
        mock_cookie_auth.return_value = MagicMock(access_token="new-google-token")
        mock_auth.return_value = MagicMock(
            access_token="new-nest-token",
            userid="user1",
            is_expired=lambda buffer_seconds=0: False,
        )
        await component_setup_with_cookies()

    # Should have fallen through to cookie auth
    mock_cookie_auth.assert_called_once()
    assert config_entry_with_cookies.state is ConfigEntryState.LOADED


async def test_startup_updates_cookies_when_refreshed(
    hass,
    component_setup_with_cookies,
    config_entry_with_cookies,
):
    """Test that refreshed cookies from Google are persisted to config entry."""
    with (
        patch(
            "custom_components.nest_protect.Store.async_load",
            return_value=None,
        ),
        patch(
            "custom_components.nest_protect.NestClient.get_access_token_from_cookies"
        ) as mock_cookie_auth,
        patch("custom_components.nest_protect.NestClient.authenticate") as mock_auth,
        patch("custom_components.nest_protect.NestClient.get_first_data") as mock_first_data,
        patch("custom_components.nest_protect.Store.async_save"),
    ):
        mock_cookie_auth.return_value = MagicMock(access_token="token")
        mock_auth.return_value = MagicMock(
            access_token="nest-token",
            userid="user1",
            is_expired=lambda buffer_seconds=0: False,
            to_dict=lambda: {"access_token": "nest-token"},
        )
        mock_first_data.return_value = MagicMock(
            updated_buckets=[], service_urls={"urls": {"transport_url": "https://t.example.com"}}
        )

        # Simulate the client having refreshed cookies after auth
        async def set_refreshed_cookies(*args, **kwargs):
            # Access the client after it's been created
            entry_data = hass.data.get("nest_protect", {}).get(config_entry_with_cookies.entry_id)
            if entry_data:
                entry_data.client.refreshed_cookies = "SID=new; HSID=new"
            return mock_cookie_auth.return_value

        mock_cookie_auth.side_effect = set_refreshed_cookies
        await component_setup_with_cookies()

    # The cookies in the config entry should have been updated
    assert config_entry_with_cookies.data.get(CONF_COOKIES) == "SID=new; HSID=new"
```

- [ ] **Step 2: Update conftest to include account_type**

The config entry fixtures need `account_type` since the new code requires it. Modify `tests/conftest.py`:

```python
REFRESH_TOKEN = "some-refresh-token"
ISSUE_TOKEN = "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&fake=true"
COOKIES = "SID=test-sid; HSID=test-hsid; APISID=test-apisid; SAPISID=test-sapisid; SSID=test-ssid"


@pytest.fixture
async def config_entry_with_refresh_token() -> MockConfigEntry:
    """Fixture to initialize a MockConfigEntry."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={"refresh_token": REFRESH_TOKEN, "account_type": "production"},
    )


@pytest.fixture
async def config_entry_with_cookies() -> MockConfigEntry:
    """Fixture to initialize a MockConfigEntry."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect pytest tests/test_init.py::test_startup_reuses_persisted_session -v`
Expected: FAIL - Store not used in `async_setup_entry`

- [ ] **Step 4: Implement the three-tier startup in __init__.py**

Replace the `async_setup_entry` function and add persistence helpers in `custom_components/nest_protect/__init__.py`:

```python
"""Nest Protect integration."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from aiohttp import ClientConnectorError, ClientError, ServerDisconnectedError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import (
    BACKOFF_INTERVALS,
    CONF_ACCOUNT_TYPE,
    CONF_COOKIES,
    CONF_ISSUE_TOKEN,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    LOGGER,
    MAX_AUTH_FAILURES,
    PLATFORMS,
    SESSION_EXPIRY_BUFFER_SECONDS,
    STORAGE_KEY_FORMAT,
    STORAGE_VERSION,
)
from .pynest.client import NestClient
from .pynest.const import NEST_ENVIRONMENTS
from .pynest.enums import BucketType, Environment
from .pynest.exceptions import (
    BadCredentialsException,
    EmptyResponseException,
    NestServiceException,
    NotAuthenticatedException,
    PynestException,
)
from .pynest.models import Bucket, FirstDataAPIResponse, NestResponse, TopazBucket, WhereBucketValue


@dataclass
class HomeAssistantNestProtectData:
    """Nest Protect data stored in the Home Assistant data object."""

    devices: dict[str, Bucket]
    areas: list[str, str]
    client: NestClient
    store: Store
    subscription_task: asyncio.Task | None = None


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    """Migrate old Config entries."""
    LOGGER.debug("Migrating from version %s", config_entry.version)

    if config_entry.version == 1:
        hass.config_entries.async_update_entry(
            config_entry,
            data={**config_entry.data, CONF_ACCOUNT_TYPE: Environment.PRODUCTION},
            version=2,
        )

    LOGGER.debug("Migration to version %s successful", config_entry.version)

    return True


async def _async_persist_session(
    store: Store, nest_session: NestResponse, transport_url: str | None
) -> None:
    """Persist Nest session to storage for reuse across restarts."""
    await store.async_save({
        "nest_session": nest_session.to_dict(),
        "transport_url": transport_url,
    })


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Nest Protect from a config entry.

    Authentication strategy (three-tier fallback):
    1. Reuse persisted Nest session if still valid (skip Google entirely)
    2. Re-authenticate with Google using stored cookies
    3. Raise ConfigEntryAuthFailed (user must re-enter credentials)
    """
    issue_token = None
    cookies = None
    refresh_token = None

    if CONF_ISSUE_TOKEN in entry.data and CONF_COOKIES in entry.data:
        issue_token = entry.data[CONF_ISSUE_TOKEN]
        cookies = entry.data[CONF_COOKIES]
    if CONF_REFRESH_TOKEN in entry.data:
        refresh_token = entry.data[CONF_REFRESH_TOKEN]

    session = async_create_clientsession(hass)
    account_type = entry.data.get(CONF_ACCOUNT_TYPE, Environment.PRODUCTION)
    client = NestClient(session=session, environment=NEST_ENVIRONMENTS[account_type])

    store = Store(hass, STORAGE_VERSION, STORAGE_KEY_FORMAT.format(entry_id=entry.entry_id))

    # --- Tier 1: Try reusing persisted Nest session ---
    nest = None
    data = None
    persisted = await store.async_load()

    if persisted and persisted.get("nest_session"):
        restored_session = NestResponse.from_dict(persisted["nest_session"])

        if restored_session and not restored_session.is_expired(
            buffer_seconds=SESSION_EXPIRY_BUFFER_SECONDS
        ):
            LOGGER.debug(
                "Reusing persisted Nest session (expires: %s)",
                restored_session.expires_in,
            )
            client.nest_session = restored_session
            client.transport_url = persisted.get("transport_url")

            # Validate the session is actually accepted by Nest
            try:
                data = await client.get_first_data(
                    restored_session.access_token, restored_session.userid
                )
                nest = restored_session
            except (NotAuthenticatedException, PynestException):
                LOGGER.debug(
                    "Persisted session rejected by Nest, falling through to cookie auth"
                )
                client.nest_session = None
                nest = None
        else:
            LOGGER.debug("Persisted session expired, falling through to cookie auth")

    # --- Tier 2: Re-authenticate with Google cookies ---
    if nest is None:
        try:
            if issue_token and cookies:
                auth = await client.get_access_token_from_cookies(issue_token, cookies)
            elif refresh_token:
                auth = await client.get_access_token_from_refresh_token(refresh_token)
            else:
                raise ConfigEntryAuthFailed("No credentials available")

            nest = await client.authenticate(auth.access_token)

            LOGGER.debug(
                "Cookie auth succeeded, cookies refreshed: %s",
                client.refreshed_cookies is not None,
            )

            # Persist the new session for next restart
            await _async_persist_session(store, nest, client.transport_url)

            # Update cookies in config entry if Google returned refreshed cookies
            if client.refreshed_cookies and client.refreshed_cookies != cookies:
                hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_COOKIES: client.refreshed_cookies},
                )

        except (TimeoutError, ClientError) as exception:
            raise ConfigEntryNotReady from exception
        except BadCredentialsException as exception:
            raise ConfigEntryAuthFailed from exception
        except Exception as exception:
            LOGGER.exception("Unknown exception.")
            raise ConfigEntryNotReady from exception

    if data is None:
        data = await client.get_first_data(nest.access_token, nest.userid)

    device_buckets: list[Bucket] = []
    areas: dict[str, str] = {}

    for bucket in data.updated_buckets:
        # Nest Protect and Temperature Sensors
        if bucket.type in {BucketType.TOPAZ, BucketType.KRYPTONITE}:
            device_buckets.append(bucket)

        # Areas
        if bucket.type == BucketType.WHERE and isinstance(
            bucket.value, WhereBucketValue
        ):
            bucket_value = bucket.value
            for area in bucket_value.wheres:
                areas[area.where_id] = area.name

    devices: dict[str, Bucket] = {b.object_key: b for b in device_buckets}

    entry_data = HomeAssistantNestProtectData(
        devices=devices,
        areas=areas,
        client=client,
        store=store,
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Subscribe for real-time updates
    entry_data.subscription_task = asyncio.create_task(
        _async_subscribe_for_data(hass, entry, data)
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Cancel subscription task only after successful platform unload
        if entry.entry_id in hass.data.get(DOMAIN, {}):
            entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
            if entry_data.subscription_task:
                entry_data.subscription_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await entry_data.subscription_task
            hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


def _register_subscribe_task(
    hass: HomeAssistant, entry: ConfigEntry, data: FirstDataAPIResponse
) -> asyncio.Task | None:
    """Create a new subscription task and update the reference."""
    # Check if entry is still loaded before creating new task
    if entry.entry_id not in hass.data.get(DOMAIN, {}):
        return None

    entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    task = asyncio.create_task(_async_subscribe_for_data(hass, entry, data))
    entry_data.subscription_task = task
    return task


async def _async_subscribe_for_data(
    hass: HomeAssistant, entry: ConfigEntry, data: FirstDataAPIResponse
):
    """Subscribe for new data.

    Uses exponential backoff on repeated failures and raises
    ConfigEntryAuthFailed after MAX_AUTH_FAILURES consecutive auth errors.
    """
    # Check if entry is still loaded
    if entry.entry_id not in hass.data.get(DOMAIN, {}):
        return

    entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    consecutive_failures = getattr(entry_data, "_consecutive_failures", 0)

    try:
        # Check for cancellation early to avoid creating orphaned tasks
        await asyncio.sleep(0)

        if (
            not entry_data.client.nest_session
            or entry_data.client.nest_session.is_expired(
                buffer_seconds=SESSION_EXPIRY_BUFFER_SECONDS
            )
        ):
            LOGGER.debug("Subscriber: authenticate for new Nest session")

            if not entry_data.client.auth or entry_data.client.auth.is_expired():
                LOGGER.debug("Subscriber: retrieving new Google access token")
                auth = await entry_data.client.get_access_token()
                entry_data.client.nest_session = await entry_data.client.authenticate(
                    auth.access_token
                )

                # Persist refreshed session for next restart
                await _async_persist_session(
                    entry_data.store,
                    entry_data.client.nest_session,
                    entry_data.client.transport_url,
                )

        # Subscribe to Google Nest subscribe endpoint
        result = await entry_data.client.subscribe_for_data(
            entry_data.client.nest_session.access_token,
            entry_data.client.nest_session.userid,
            data.service_urls["urls"]["transport_url"],
            data.updated_buckets,
        )

        # Reset failure counter on success
        entry_data._consecutive_failures = 0

        for bucket in result["objects"]:
            key = bucket["object_key"]

            # Nest Protect
            if key.startswith("topaz."):
                topaz = TopazBucket(**bucket)
                entry_data.devices[key] = topaz

                async_dispatcher_send(hass, key, topaz)

            # Areas
            if key.startswith("where."):
                bucket_value = Bucket(**bucket).value

                for area in bucket_value.wheres:
                    entry_data.areas[area.where_id] = area.name

            # Temperature Sensors
            if key.startswith("kryptonite."):
                kryptonite = Bucket(**bucket)
                entry_data.devices[key] = kryptonite

                async_dispatcher_send(hass, key, kryptonite)

        # Update buckets with new data, to only receive new updates
        buckets = {d["object_key"]: d for d in result["objects"]}

        LOGGER.debug(buckets)

        objects = [
            dict(vars(b), **buckets.get(b.object_key, {})) for b in data.updated_buckets
        ]

        data.updated_buckets = [
            Bucket(
                object_key=bucket["object_key"],
                object_revision=bucket["object_revision"],
                object_timestamp=bucket["object_timestamp"],
                value=bucket["value"],
                type=bucket["type"],
            )
            for bucket in objects
        ]

        _register_subscribe_task(hass, entry, data)
    except ServerDisconnectedError:
        LOGGER.debug("Subscriber: server disconnected.")
        _register_subscribe_task(hass, entry, data)

    except asyncio.exceptions.TimeoutError:
        LOGGER.debug("Subscriber: session timed out.")
        _register_subscribe_task(hass, entry, data)

    except ClientConnectorError:
        LOGGER.debug("Subscriber: cannot connect to host.")
        _register_subscribe_task(hass, entry, data)

    except EmptyResponseException:
        LOGGER.debug("Subscriber: Nest Service sent empty response.")
        _register_subscribe_task(hass, entry, data)

    except NotAuthenticatedException:
        LOGGER.debug("Subscriber: 401 exception.")
        consecutive_failures += 1
        entry_data._consecutive_failures = consecutive_failures

        if consecutive_failures >= MAX_AUTH_FAILURES:
            LOGGER.warning(
                "Subscriber: %d consecutive auth failures, triggering re-authentication",
                consecutive_failures,
            )
            raise ConfigEntryAuthFailed(
                f"{consecutive_failures} consecutive authentication failures"
            )

        # Renewing access token with backoff
        backoff = BACKOFF_INTERVALS[min(consecutive_failures - 1, len(BACKOFF_INTERVALS) - 1)]
        LOGGER.debug("Subscriber: retrying in %ds (attempt %d)", backoff, consecutive_failures)
        await asyncio.sleep(backoff)

        await entry_data.client.get_access_token()
        await entry_data.client.authenticate(entry_data.client.auth.access_token)

        # Persist refreshed session
        if entry_data.client.nest_session:
            await _async_persist_session(
                entry_data.store,
                entry_data.client.nest_session,
                entry_data.client.transport_url,
            )

        _register_subscribe_task(hass, entry, data)

    except BadCredentialsException as exception:
        LOGGER.debug(
            "Bad credentials detected. Please re-authenticate the Nest Protect integration."
        )
        raise ConfigEntryAuthFailed from exception

    except NestServiceException:
        LOGGER.debug("Subscriber: Nest Service error. Updates paused for 2 minutes.")

        await asyncio.sleep(60 * 2)
        _register_subscribe_task(hass, entry, data)

    except PynestException:
        LOGGER.exception(
            "Unknown pynest exception. Please create an issue on GitHub with your logfile. Updates paused for 1 minute."
        )

        # Wait a minute before retrying
        await asyncio.sleep(60)
        _register_subscribe_task(hass, entry, data)

    except asyncio.CancelledError:
        # Task is being cancelled during unload; do not register a new task
        LOGGER.debug("Subscriber: task cancelled, stopping subscription.")
        raise

    except Exception:
        consecutive_failures += 1
        entry_data._consecutive_failures = consecutive_failures
        backoff = BACKOFF_INTERVALS[min(consecutive_failures - 1, len(BACKOFF_INTERVALS) - 1)]

        LOGGER.exception(
            "Unknown exception. Please create an issue on GitHub with your logfile. Updates paused for %ds.",
            backoff,
        )

        await asyncio.sleep(backoff)
        _register_subscribe_task(hass, entry, data)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Remove a config entry from a device."""
    return True
```

- [ ] **Step 5: Run all tests to verify they pass**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect pytest tests/ -v`
Expected: All tests PASS (new persistence tests + existing tests)

- [ ] **Step 6: Commit**

```bash
git add custom_components/nest_protect/__init__.py custom_components/nest_protect/const.py tests/conftest.py tests/test_init.py
git commit -m "feat: three-tier auth startup with session persistence and exponential backoff

Persist Nest session across restarts using HA Store. On startup:
1. Reuse persisted session if still valid
2. Fall back to cookie auth (with cookie refresh)
3. Raise ConfigEntryAuthFailed

Also adds exponential backoff in subscription loop and triggers
re-auth after 3 consecutive auth failures.

Fixes #459, #470, #474, #464"
```

---

### Task 5: Storage Cleanup on Entry Removal

**Files:**
- Modify: `custom_components/nest_protect/__init__.py`

- [ ] **Step 1: Add async_remove_entry to clear stored session when integration is removed**

Add after `async_unload_entry` in `__init__.py`:

```python
async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up persisted session data when the config entry is removed."""
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY_FORMAT.format(entry_id=entry.entry_id))
    await store.async_remove()
```

- [ ] **Step 2: Run all tests**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add custom_components/nest_protect/__init__.py
git commit -m "feat: clean up persisted session on integration removal"
```

---

### Task 6: Lint and Final Validation

- [ ] **Step 1: Run ruff check**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect ruff check custom_components/ tests/`
Expected: No errors (fix any that appear)

- [ ] **Step 2: Run ruff format**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect ruff format custom_components/ tests/`
Expected: Files formatted

- [ ] **Step 3: Run full test suite**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 4: Commit any formatting fixes**

```bash
git add -u
git commit -m "style: apply ruff formatting"
```
