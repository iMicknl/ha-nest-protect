# Nest Protect Session Persistence Design

## Problem

Authentication sessions expire on every Home Assistant restart (issues #459, #470, #474, #464). The integration stores cookies in the config entry but discards the active Nest session (valid ~1h) on shutdown. On startup, it always re-authenticates with Google using cookies. If Google has invalidated those cookies server-side, auth fails with `USER_LOGGED_OUT` and the user must manually re-enter credentials.

## Solution

Persist the Nest session and Google auth token across restarts using HA's `Store` helper. On startup, attempt to reuse the persisted session before falling back to cookie auth. When cookie auth succeeds, capture refreshed cookies from Google's response headers and update the config entry.

## Authentication Strategy (Three-Tier Fallback)

```
1. Reuse persisted Nest session (if not expired, with 5-min buffer)
   |-- Try get_first_data; if 401, fall through
   v
2. Cookie-based Google auth (existing flow)
   |-- On success: persist new session + update cookies if refreshed
   v
3. ConfigEntryAuthFailed (user must re-authenticate)
```

## Persistence Mechanism

### Nest Session (Store)

Use `homeassistant.helpers.storage.Store` with key `nest_protect_{entry_id}` (version 1).

Persisted data:
```json
{
  "nest_session": {
    "access_token": "...",
    "email": "...",
    "expires_in": "Tue, 01-Mar-2022 23:15:55 GMT",
    "userid": "...",
    "is_superuser": false,
    "language": "...",
    "weave": {},
    "user": "...",
    "is_staff": false
  },
  "transport_url": "..."
}
```

Saved after:
- Successful authentication during startup (tier 2)
- Successful token refresh in the subscription loop

Loaded during:
- `async_setup_entry` (tier 1 check)

Cleared when:
- Config entry is removed
- Authentication permanently fails (triggers re-auth flow)

### Cookie Refresh (Config Entry)

After a successful `get_access_token_from_cookies` call:
1. Extract `Set-Cookie` headers from the Google OAuth response
2. Merge new cookie values into the existing cookie string (new values override same-name cookies)
3. If merged string differs from stored, update `config_entry.data[CONF_COOKIES]`

Design decisions:
- We do NOT use aiohttp's cookie jar. The user-provided cookie string contains cookies spanning multiple Google domains/paths. The jar's domain-scoping would break this.
- We do NOT persist cookies from Nest API calls. Nest uses Bearer token auth; its cookies are decorative. Only Google OAuth cookies matter for re-authentication.

## Robustness Improvements

### Expiry Buffer

Treat the Nest session as expired when within 5 minutes of its stated expiry. Avoids races where the session expires between validation and API use.

### Persisted Session Validation

Don't blindly trust a persisted session's timestamp. After loading, attempt `get_first_data`. If it returns 401, discard the session and fall through to cookie auth.

### Error Differentiation

Currently all auth exceptions get the same treatment. Improve to:
- `USER_LOGGED_OUT` / `invalid_grant` -> `ConfigEntryAuthFailed` (permanent failure, user re-auth)
- HTTP 5xx / timeouts / network errors -> `ConfigEntryNotReady` (HA auto-retries)
- HTTP 429 -> `ConfigEntryNotReady` + warning log

### Subscription Loop Resilience

- Exponential backoff on repeated failures (30s, 60s, 120s, 300s, cap at 600s)
- After 3 consecutive auth failures, raise `ConfigEntryAuthFailed` instead of silently retrying

### Startup Logging

Debug-level logs for each tier:
- "Reusing persisted Nest session (expires in X min)"
- "Persisted session expired/invalid, re-authenticating with Google cookies"
- "Cookie auth succeeded, cookies refreshed: yes/no"

## File Changes

### Modified

| File | Changes |
|------|---------|
| `__init__.py` | Three-tier startup, Store load/save, cookie update, subscription persistence, exponential backoff |
| `pynest/client.py` | Return refreshed cookies from `get_access_token_from_cookies`, add cookie merge helper |
| `pynest/models.py` | Add `to_dict()` / `from_dict()` to `NestResponse` for serialization |
| `const.py` | Add `STORAGE_VERSION`, `STORAGE_KEY` constants |

### Not Modified

| File | Reason |
|------|--------|
| `config_flow.py` | Initial credential capture unchanged |
| Entity files | Consume `entry_data.client` which remains the same interface |

## Constants

```python
STORAGE_VERSION = 1
STORAGE_KEY = "nest_protect_{entry_id}"
SESSION_EXPIRY_BUFFER_SECONDS = 300  # 5 minutes
MAX_AUTH_FAILURES = 3
```
