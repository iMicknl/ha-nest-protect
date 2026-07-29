# Authentication Session Coordinator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one extensible, serialized authentication coordinator that
durably saves credential rotations before downstream Nest calls and only starts
reauthentication after repeated explicit credential rejection.

**Architecture:** Credential-specific Google token acquisition moves behind a
small provider protocol in `credentials.py`. `NestSessionManager` owns provider
fallback, bounded rejection retries, single-flight Nest-session refresh, and a
merged durable state record. Runtime callers only request or recover a Nest
session.

**Tech Stack:** Python 3.14, asyncio, aiohttp, Home Assistant config entries and
Store, pytest, pytest-asyncio, Ruff.

## Global Constraints

- Preserve cookie and legacy refresh-token authentication.
- New authentication methods require a provider registration, not edits to
  every runtime consumer.
- Persist provider credential changes before any dependent Nest request.
- Serialize all Google-token and Nest-session changes.
- Never start reauthentication for network, 429, 5xx, or malformed responses.
- Tag durable auth state with a login generation so reauth cannot restore a
  late write from the manager being replaced.
- Do not log credential values.
- Keep provider logic in `credentials.py`, session policy in `session.py`, and
  domain operations at their existing call sites.
- Execute every command through the devcontainer CLI.

---

### Task 1: Credential provider boundary

**Files:**
- Create: `custom_components/nest_protect/credentials.py`
- Create: `tests/test_credentials.py`
- Modify: `custom_components/nest_protect/const.py`

**Interfaces:**
- Produces: `CredentialProvider`, `CredentialUpdateCallback`,
  `CookieCredentialProvider`, `RefreshTokenCredentialProvider`, and
  `create_credential_providers(client, async_save_credentials)`.
- Consumes: existing `NestClient` cookie and refresh-token methods.

- [ ] **Step 1: Write failing provider behavior tests**

```python
async def test_cookie_provider_saves_rotation_when_request_is_rejected():
    client = MagicMock(cookies="SID=old", refreshed_cookies=None)

    async def reject(*_):
        client.cookies = "SID=new"
        client.refreshed_cookies = "SID=new"
        raise BadCredentialsException("USER_LOGGED_OUT")

    client.get_access_token_from_cookies = AsyncMock(side_effect=reject)
    saved = AsyncMock()
    provider = CookieCredentialProvider(client, saved)

    with pytest.raises(BadCredentialsException):
        await provider.async_get_access_token()

    assert saved.await_args.args[0] == {CONF_COOKIES: "SID=new"}


async def test_refresh_token_provider_remains_supported():
    client = MagicMock(refresh_token="legacy-token")
    client.get_access_token_from_refresh_token = AsyncMock(
        return_value=MagicMock(access_token="google-token")
    )

    provider = RefreshTokenCredentialProvider(client, AsyncMock())

    assert (await provider.async_get_access_token()).access_token == "google-token"
```

- [ ] **Step 2: Verify the tests fail because the module is absent**

Run:

```bash
devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect \
  sh -lc 'cd /workspaces/ha-nest-protect/config/codex-worktrees/auth-session-coordinator && /tmp/ha-nest-protect-maint-baseline/.venv-auth-investigation/bin/pytest -q tests/test_credentials.py'
```

Expected: collection fails because `credentials.py` does not exist.

- [ ] **Step 3: Implement the provider protocol and providers**

```python
CredentialUpdateCallback = Callable[[Mapping[str, Any]], Awaitable[None]]


class CredentialProvider(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    async def async_get_access_token(self) -> GoogleAuthResponse: ...


class CookieCredentialProvider:
    name = "cookies"

    async def async_get_access_token(self) -> GoogleAuthResponse:
        previous = self._client.cookies
        try:
            return await self._client.get_access_token_from_cookies(
                self._client.issue_token, previous
            )
        finally:
            refreshed = self._client.refreshed_cookies
            if refreshed and refreshed != previous:
                await self._async_save_credentials({CONF_COOKIES: refreshed})
```

Implement the refresh-token provider and factory with cookie-first ordering.
Add `AUTH_RETRY_DELAYS = (1, 5)` to `const.py`.

- [ ] **Step 4: Run provider tests and Ruff**

Run the focused pytest command from Step 2, followed by:

```bash
devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect \
  sh -lc 'cd /workspaces/ha-nest-protect/config/codex-worktrees/auth-session-coordinator && /tmp/ha-nest-protect-maint-baseline/.venv-auth-investigation/bin/ruff check custom_components/nest_protect/credentials.py tests/test_credentials.py'
```

Expected: all provider tests pass and Ruff exits zero.

### Task 2: Durable, single-flight session orchestration

**Files:**
- Modify: `custom_components/nest_protect/session.py`
- Modify: `tests/test_session.py`

**Interfaces:**
- Consumes: `CredentialProvider` and `create_credential_providers` from Task 1.
- Produces:
  `NestSessionManager(..., credential_update_callback=None, retry_delays=...)`,
  `ensure_session()`, and
  `async_refresh_session(rejected_session: NestResponse | None = None)`.

- [ ] **Step 1: Add failing durable-ordering and restart tests**

Use a small `MemoryStore` that copies values on save/load. Exercise the real
cookie provider and session manager while mocking only network operations.

```python
async def test_rotation_survives_nest_failure_and_restart():
    first_client = make_cookie_client("SID=old")

    async def rotate(*_):
        first_client.cookies = "SID=new"
        first_client.refreshed_cookies = "SID=new"
        return MagicMock(access_token="google-token")

    first_client.get_access_token_from_cookies.side_effect = rotate
    first_client.authenticate.side_effect = PynestException("Nest unavailable")
    store = MemoryStore()
    manager = NestSessionManager(first_client, store, retry_delays=())

    with pytest.raises(PynestException):
        await manager.async_setup()

    restarted_client = make_cookie_client("SID=old")
    restarted_manager = NestSessionManager(restarted_client, store, retry_delays=())
    restarted_client.authenticate.side_effect = PynestException("stop")

    with pytest.raises(PynestException):
        await restarted_manager.async_setup()

    assert (
        restarted_client.get_access_token_from_cookies.await_args.args[1] == "SID=new"
    )
```

Also add tests proving:

- a transient persisted-session failure does not invoke providers;
- cookie rejection falls back to refresh-token authentication;
- all providers must explicitly reject on every bounded attempt before
  `BadCredentialsException` is raised;
- concurrent `ensure_session()` calls perform one refresh;
- two callers reporting the same rejected Nest session perform one refresh;
- a stored credential update is applied before persisted-session validation.

- [ ] **Step 2: Run each new test and confirm the expected current failure**

Run each test by node ID. Expected failures must identify missing durable
credentials, missing locking, or missing provider fallback rather than fixture
errors.

- [ ] **Step 3: Implement merged durable state**

Load the Store once into `_stored_state`. Preserve unknown keys. Save credential
updates under `credentials`, and await the Store write before invoking the
config-entry callback.

```python
async def _async_save_credentials(self, updates: Mapping[str, Any]) -> None:
    await self._async_load_state()
    credentials = dict(self._stored_state.get("credentials", {}))
    credentials.update(updates)
    self._stored_state["credentials"] = credentials
    await self._store.async_save(self._stored_state)
    apply_credential_updates(self._client, updates)
    if self._credential_update_callback:
        await self._credential_update_callback(updates)
```

- [ ] **Step 4: Implement serialization, provider fallback, and rejection retry**

Guard setup and refresh with one `asyncio.Lock`. Re-check session state inside
the lock. Try an unexpired cached Google token once, then each provider.
Continue to the next provider only for explicit credential or Nest-token
rejection. Retry a complete all-provider rejection using `retry_delays`.

Invoke the optional reauthentication callback only when every attempt ended in
explicit provider rejection.

- [ ] **Step 5: Narrow persisted-session fallback**

Only `NotAuthenticatedException` invalidates a restored session and falls back
to credentials. Transient and malformed Nest responses propagate without
touching credentials.

- [ ] **Step 6: Run the full session test file and Ruff**

Expected: all session tests pass; old call-site failure-counter tests will be
removed in Task 5 when the callers migrate.

- [ ] **Step 7: Protect reauthentication from stale Store writes**

Persist an opaque authentication generation before the first external request.
Adopt a durable generation when an upgraded config entry has not mirrored it
yet. When the config entry and Store generations differ, keep the config-entry
generation and discard all older durable credentials and sessions. For manual
reauthentication, write a predecessor-linked handoff before the delayed
config-entry update and retire the old manager under its session lock.

### Task 3: HTTP authentication error classification

**Files:**
- Modify: `custom_components/nest_protect/pynest/client.py`
- Modify: `custom_components/nest_protect/pynest/exceptions.py`
- Modify: `tests/pynest/test_client.py`

**Interfaces:**
- Produces consistent `NotAuthenticatedException`,
  `NestServiceException`, `BadCredentialsException`, and `PynestException`
  outcomes from Google-token, Nest-session, and app-launch endpoints.

- [ ] **Step 1: Add failing response classification tests**

Use the existing aiohttp test server to cover JSON and text responses:

```python
@pytest.mark.parametrize("status", [401, 403])
async def test_get_first_data_classifies_auth_status(status, socket_enabled):
    ...
    with pytest.raises(NotAuthenticatedException):
        await client.get_first_data("token", "user")


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_get_first_data_classifies_transient_status(status, socket_enabled):
    ...
    with pytest.raises(NestServiceException):
        await client.get_first_data("token", "user")
```

Add equivalent coverage for `/session`, and confirm unknown Google errors raise
`PynestException` rather than a bare `Exception`.

- [ ] **Step 2: Run the new tests and verify their current exception mismatch**

Expected: tests fail with `ContentTypeError`, `PynestException`, or a bare
`Exception`.

- [ ] **Step 3: Add one response-status helper and use it before success decoding**

```python
async def _raise_for_nest_status(response: ClientResponse, action: str) -> None:
    if response.status in {401, 403}:
        raise NotAuthenticatedException(await response.text())
    if response.status == 429 or response.status >= 500:
        raise NestServiceException(
            f"{response.status} error while {action} - {await response.text()}"
        )
```

Keep explicit Google `USER_LOGGED_OUT` and `invalid_grant` mapping to
`BadCredentialsException`; map other error payloads to `PynestException`.

- [ ] **Step 4: Run client tests and Ruff**

Expected: all client tests pass and error payloads contain no credentials.

### Task 4: Home Assistant persistence and configuration integration

**Files:**
- Modify: `custom_components/nest_protect/__init__.py`
- Modify: `custom_components/nest_protect/config_flow.py`
- Modify: `tests/test_init.py`
- Modify: `tests/test_config_flow.py`

**Interfaces:**
- Consumes: manager credential callback and provider factory.
- Produces: config-entry mirroring, reauth callback registration, config-flow
  provider validation, and removal of stale Store state after manual reauth.

- [ ] **Step 1: Add failing setup persistence tests**

Cover a rotation followed by Nest failure and assert the per-entry Store
contains the new cookie even though the config entry remains in setup retry.
Then create a fresh setup with the same Store and assert it sends the new
cookie.

- [ ] **Step 2: Add failing config-flow tests**

Assert refresh-token validation still succeeds through the provider factory,
cookie rotations are returned, and a downstream Nest failure reuses a captured
rotation on the next form attempt. Assign a fresh authentication generation,
stage the credentials durably before config-entry mutation, and fence every
late callback from the replaced manager.

- [ ] **Step 3: Wire the manager callback**

Create an async callback in `async_setup_entry` that updates the live client and
config entry. Pass it to `NestSessionManager`. Remove
`_persist_refreshed_cookies` and every caller of it.

After successful setup, attach:

```python
session_manager.set_reauthentication_callback(lambda: entry.async_start_reauth(hass))
```

- [ ] **Step 4: Use providers in config-flow validation**

Initialize the client from submitted credentials, select the first available
provider, capture updates in a local mapping, and validate the Nest session.
Retain rotations across form retries. Stage successful reauth data and its new
generation in Store before updating the config entry.

- [ ] **Step 5: Run init/config-flow tests and Ruff**

Expected: both cookie and refresh-token fixtures load, rotated data is durable,
and no old post-Nest-call cookie copy remains.

### Task 5: Migrate every runtime consumer

**Files:**
- Modify: `custom_components/nest_protect/__init__.py`
- Modify: `custom_components/nest_protect/diagnostics.py`
- Modify: `custom_components/nest_protect/entity.py`
- Modify: `custom_components/nest_protect/lock.py`
- Modify: affected tests under `tests/`
- Create: `tests/test_diagnostics.py` if diagnostics has no existing test file.

**Interfaces:**
- Consumes: `ensure_session()` and
  `async_refresh_session(rejected_session=...)`.
- Produces: no direct runtime calls to Google token methods outside the
  credential providers.

- [ ] **Step 1: Add failing consumer tests**

Assert:

- diagnostics calls `ensure_session` and never calls Google token methods;
- an updatable entity retries once after `NotAuthenticatedException`;
- a lock command ensures a session and retries once after
  `NestLockAuthException`;
- subscriber and lock observer pass the exact rejected session object;
- two consumer failures for the same token cause one provider refresh;
- transient refresh failure schedules recovery without starting reauth;
- exhausted explicit credential rejection starts reauth once.

- [ ] **Step 2: Run each focused test and verify current behavior fails**

Expected: direct-auth diagnostics, missing command retry, old shared counters, or
duplicate refresh calls cause the failures.

- [ ] **Step 3: Migrate diagnostics and entity operations**

Diagnostics uses the existing Nest session after `ensure_session`.
`NestUpdatableEntity` retries the domain request once after coordinated
recovery.

- [ ] **Step 4: Migrate subscriber and lock observer**

Capture the Nest access token used by each operation. On a 401/403, ask the
manager to replace that token. Remove per-call-site auth failure counters and
cookie persistence.

- [ ] **Step 5: Migrate lock commands**

Pass `NestSessionManager` to `NestLockEntity`, ensure the session before a
command, and retry one time after coordinated recovery. Preserve existing
`HomeAssistantError` behavior for final failure.

- [ ] **Step 6: Prove no runtime auth bypass remains**

Run:

```bash
devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect \
  sh -lc 'cd /workspaces/ha-nest-protect/config/codex-worktrees/auth-session-coordinator && git grep -n "get_access_token_from_cookies\\|get_access_token_from_refresh_token" -- custom_components/nest_protect'
```

Expected matches: credential providers, config-flow setup code if needed, and
low-level client definitions only.

- [ ] **Step 7: Run all consumer tests and Ruff**

Expected: focused tests pass without credential-value logging or sleeps longer
than the injected retry policy.

### Task 6: Full verification and readability review

**Files:**
- Modify only files needed to correct verification findings.

**Interfaces:**
- Consumes: all earlier tasks.
- Produces: releasable branch evidence.

- [ ] **Step 1: Run formatter and lint**

```bash
devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect \
  sh -lc 'cd /workspaces/ha-nest-protect/config/codex-worktrees/auth-session-coordinator && /tmp/ha-nest-protect-maint-baseline/.venv-auth-investigation/bin/ruff format --check . && /tmp/ha-nest-protect-maint-baseline/.venv-auth-investigation/bin/ruff check .'
```

- [ ] **Step 2: Run the complete test suite**

```bash
devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect \
  sh -lc 'cd /workspaces/ha-nest-protect/config/codex-worktrees/auth-session-coordinator && /tmp/ha-nest-protect-maint-baseline/.venv-auth-investigation/bin/pytest -q'
```

- [ ] **Step 3: Run restart, rejection, and concurrency regression tests alone**

Run the named tests from Tasks 2, 4, and 5 with `-vv` so their individual
results are visible.

- [ ] **Step 4: Review the final diff**

Check that:

- `session.py` is the only runtime authentication policy owner;
- credential providers do not import Home Assistant objects;
- every credential update is saved before Nest authentication;
- token-only entries have explicit test coverage;
- transient exceptions cannot call `async_start_reauth`;
- no credential values appear in logs or test failure output;
- comments explain only non-obvious ordering and compatibility constraints.

- [ ] **Step 5: Run `git diff --check` and report exact verification counts**

Do not claim completion unless every fresh command exits zero.
