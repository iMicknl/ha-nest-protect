# Authentication Session Coordinator Design

## Goal

Make authentication recovery deterministic across Home Assistant restarts,
concurrent consumers, transient service failures, and credential rotation while
keeping cookie, refresh-token, and future authentication methods isolated and
readable.

The integration can eliminate false reauthentication and lost credential
updates. It cannot make credentials valid after Google revokes them or binds
them to another device, so a confirmed rejection still ends in Home Assistant's
reauthentication flow.

## Problems Being Solved

The current implementation splits ownership across layers:

- `NestClient` receives and applies rotated Google cookies in memory.
- `NestSessionManager` creates and stores Nest sessions.
- setup, subscriber, and lock call sites sometimes copy rotated cookies into
  the config entry after unrelated Nest calls complete.
- diagnostics performs its own authentication and does not persist rotations.
- entity and lock operations can race the subscriber for the same mutable
  client state.

This leaves several reproducible failure windows:

1. Google rotates cookies, a subsequent Nest call fails, and Home Assistant
   restarts before the caller persists the new cookies.
2. Two consumers refresh concurrently and overwrite or repeat the same work.
3. Subscriber and lock failures from one rejected session are counted as
   independent credential failures.
4. A transient Nest response is interpreted as credential rejection.
5. Runtime paths bypass the persistence behavior added to other call sites.

## Approaches Considered

### Patch every call site

Each caller would continue authenticating directly and would persist cookies in
a `finally` block. This is a small diff, but every new feature would need to
repeat locking, persistence, fallback, retry, and classification correctly.
This is the pattern that created the current gaps, so it is rejected.

### Put Home Assistant persistence inside `NestClient`

The low-level client could save credentials whenever an HTTP response rotates
them. This closes the timing window but couples the reusable protocol client to
Home Assistant config entries and storage. It also makes config-flow validation
and testing harder. This is rejected.

### Provider-based session coordinator

Credential providers acquire Google access tokens and report provider-specific
durable changes. `NestSessionManager` owns serialization, retry and fallback,
Nest-session persistence, and the ordering of durable writes. Home Assistant
supplies a small callback that mirrors durable credential updates into the
config entry. This is the selected design.

## Components

### Credential providers

A `CredentialProvider` protocol exposes:

- a human-readable provider name for safe logging;
- the config-entry fields owned by that provider;
- whether its required credentials are available;
- an async method that returns a Google access token.

Providers receive a callback for credential updates. A provider invokes that
callback before returning or re-raising, so response-side rotations are not
lost when a later step fails.

Initial implementations:

- `CookieCredentialProvider` uses `issue_token` and cookies. It durably saves
  any cookie rotation observed by `NestClient`, including rotations returned
  alongside a rejected response.
- `RefreshTokenCredentialProvider` uses the legacy refresh token and produces
  no update today. The interface permits future refresh-token rotation.

The provider factory preserves the existing preference for cookie credentials
when both methods are present, then falls back to the refresh token. Runtime
and config-flow validation call the same complete provider-pipeline helper.
Adding a future method requires a provider and one factory registration rather
than changes in every authentication consumer. Provider-declared fields also
drive client hydration, durable replay, credential retirement, and config-flow
pending-state identity.

### Nest session manager

`NestSessionManager` becomes the only runtime authentication coordinator. It
owns:

- one `asyncio.Lock` covering all Google-token and Nest-session changes;
- the ordered credential providers;
- a cached Google access token fast path;
- persisted Nest session validation;
- durable credential and Nest-session state;
- bounded credential rejection retries;
- de-duplication when multiple consumers report the same rejected session
  object, even if Nest issues the same token value again;
- one optional runtime callback that starts reauthentication after confirmed
  credential rejection.

Public operations remain small:

- `async_setup()` restores credentials and a Nest session, or creates and
  validates a new session.
- `ensure_session()` returns an unexpired session, refreshing under the lock
  when necessary.
- `async_refresh_session(rejected_session=...)` replaces a rejected session. If
  another consumer already replaced that exact session object, it reuses the
  new session instead of refreshing again.
- `async_invalidate_session(rejected_session=...)` removes a replacement that
  the retrying consumer also rejects, so it cannot become a bad persisted fast
  path on the next restart.
- `set_reauthentication_callback()` attaches Home Assistant behavior only
  after setup succeeds.

### Durable state

The existing per-entry `Store` remains the durable session record and gains a
generic `credentials` mapping plus an opaque authentication-generation marker.
When a provider reports an update, the manager:

1. updates its in-memory stored state;
2. awaits `Store.async_save`;
3. applies the update to the live client;
4. mirrors it to the config entry through the callback;
5. only then proceeds to Nest authentication.

The Store write closes the one-second delayed config-entry-save crash window.
On startup, stored credential updates are applied before any authentication
attempt. The generation marker handles both sides of the remaining race:

- an upgraded entry without a marker adopts the durable marker, so a crash
  between Store and config-entry writes does not discard a valid rotation;
- successful manual reauthentication assigns a new marker and records the
  previous marker in a durable handoff, so the Store can finish the new login
  after a crash that occurs before Home Assistant's delayed config-entry save;
- that predecessor marker authorizes exactly one generation transition through
  the config-entry mirror fence, after which ordinary generation checks resume;
- the old manager is retired while holding its session lock, and every
  config-entry update is tagged with its generation, so late callbacks cannot
  overwrite the new login.

The durable handoff is written before the config entry is updated. On restart,
a Store generation whose predecessor matches the config entry is recognized as
a committed reauthentication and wins. Other mismatches mean the config entry
is authoritative and stale Store credentials are discarded. The handoff is
built and saved before the active manager is retired, so a storage failure
leaves the working generation and session untouched.

Unknown Store fields are preserved to keep future state additions
forward-compatible.

## Authentication Flow

### Startup

1. Load durable state and reconcile its authentication generation.
2. Discard durable credentials and sessions from an older login generation.
3. Apply stored credential updates for the current generation.
4. If a non-expired Nest session exists, validate it with `app_launch`.
5. Fall back to credential providers only for an explicit Nest 401/403.
6. Propagate network errors, rate limits, server failures, and malformed
   responses as transient setup failures.
7. Try each available credential provider's complete token, Nest-session, and
   `app_launch` pipeline in order.
8. Persist provider rotations before authenticating with Nest.
9. Clear every candidate session whose validation fails, including on
   transient errors and cancellation.
10. Persist the Nest session only after validation succeeds.

### Runtime refresh

1. Acquire the session lock and re-check the current session.
2. If the caller supplied a rejected session object and a different valid
   session object is already installed, reuse it.
3. Clear the rejected or expired persisted Nest session.
4. For an expired session, try an unexpired cached Google access token once.
   For an explicitly rejected session, skip the Google token that created it.
5. If Nest rejects the cached token, acquire a new token through providers.
6. Persist provider updates before calling Nest.
7. Validate the new session with `app_launch`.
8. Persist it only after validation succeeds, then release the lock.

### Credential fallback and retry

A complete attempt tries every available provider. A provider rejected with
`USER_LOGGED_OUT` or `invalid_grant` does not prevent a later provider from
succeeding. A provider-specific transient failure also permits an independent
provider to succeed, but if no provider succeeds the transient outcome takes
precedence and reauthentication is not started.

Provider fallback covers the whole pipeline, not only Google token acquisition.
For example, if cookie credentials produce a Nest session that `app_launch`
rejects, the legacy refresh-token provider still gets its own complete attempt.

After every available provider explicitly rejects its credentials, the manager
repeats the complete attempt after 1, 5, and 30 seconds. It starts
reauthentication only after all four attempts are explicit credential
rejections. If any provider produces an access token but Nest rejects it, the
outcome is not classified as bad stored credentials.

Retries are not applied blindly:

| Outcome | Coordinator action |
| --- | --- |
| Explicit Google `USER_LOGGED_OUT` / `invalid_grant` | Try other providers, then bounded retry; reauth only after exhaustion |
| Google 401/403 without an explicit rejection code | Treat as transient; never start reauth |
| Nest session 401/403 | Refresh the session; do not condemn stored credentials |
| HTTP 429, 5xx, timeout, connection failure | Preserve credentials and retry later; never start reauth |
| Unexpected or malformed response | Preserve credentials, log safely, and retry later |
| Successful API operation | Continue; no failure counter is maintained at individual call sites |

## Runtime Consumers

All runtime consumers use the same manager:

- setup uses `async_setup`;
- subscriber and lock observer pass the exact failed Nest session object to
  `async_refresh_session`;
- updatable entities retry one command after coordinated session recovery;
- lock commands ensure a session and retry once after coordinated recovery;
- lock observe and command requests receive the same captured session object
  that their recovery call will report, avoiding mutable-client races;
- diagnostics uses `ensure_session` and no longer performs direct Google
  authentication; its actual `app_launch` request also refreshes once on 401.

No runtime caller reads or persists `refreshed_cookies`.

## Low-Level Error Classification

`NestClient` will classify HTTP responses before decoding success payloads:

- 401 and 403 from Nest endpoints raise `NotAuthenticatedException`;
- 429 and 5xx responses raise `NestServiceException`;
- explicit Google credential rejection raises `BadCredentialsException`;
- other invalid responses raise `PynestException`.

This prevents text/plain 401 responses and JSON `access_denied` responses from
taking different recovery paths. The same classification applies to subscribe
and update endpoints, including auth errors inside HTTP-200 JSON envelopes.
Nest x Yale gRPC status code 16 is also mapped to the coordinated auth path.

## Configuration Flow

Config-flow validation uses the same provider factory without a session Store.
It captures provider updates immediately and retains them across form retries,
so a Nest failure after Google rotates cookies does not replay the submitted
stale cookie set. During reauthentication, those updates are also written to a
separate per-entry pending Store before the downstream Nest call. A fresh flow
after a Home Assistant restart adopts them only when a fingerprint of the
resubmitted provider-owned fields and the originating authentication generation
matches, preventing an unrelated or completed login attempt from receiving
stale pending data.

On reauthentication, replacement credentials and a fresh authentication
generation are staged durably before updating the config entry. A live old
manager is retired under its session lock; if no manager is loaded, the config
flow writes the same handoff directly. Pending cleanup is best-effort and
cannot prevent the required reload. Provider-owned fields from the previous
method are removed, so switching a legacy refresh-token entry to cookies cannot
silently reactivate the superseded token.

## Testing

Tests will cover:

- cookie rotation is saved before a subsequent Nest failure;
- a fresh manager after that failure restores and uses the rotated cookie;
- rotations accompanying a rejected Google response are saved before retry;
- a rotation during failed reauthentication survives a process restart and is
  scoped to the matching submitted credential source;
- config-flow validation and runtime use the same complete provider fallback;
- a future provider's declared fields participate in hydration, durable replay,
  and pending-state isolation without coordinator changes;
- generation creation is durable before the first external auth request;
- a crash before config-entry mirroring adopts the durable generation;
- a new reauth generation rejects late durable writes from the old manager;
- a hard restart during reauth completes from the staged durable handoff;
- a failed handoff Store write leaves the old manager active;
- unrelated future Store metadata survives reauthentication;
- the predecessor-linked handoff advances the real config-entry generation
  fence and does not persist its internal predecessor marker;
- refresh-token-only entries still work;
- switching a refresh-token entry to cookie credentials retires the old token;
- a rejected cookie provider falls back to a refresh-token provider;
- failures during the cookie provider's Nest exchange or `app_launch`
  validation fall back to the refresh-token provider;
- a failed candidate session is removed from the live client on every
  validation-failure path;
- all explicit credential rejections are bounded and eventually request
  reauthentication;
- transient failures never request reauthentication;
- concurrent `ensure_session` calls perform one refresh;
- concurrent reports of the same rejected session perform one refresh;
- persisted-session transient failures do not fall back to credentials;
- setup, subscriber, lock observer, entity, lock command, and diagnostics paths
  all use the coordinator;
- JSON and non-JSON 401, 429, and 5xx responses receive the intended exception
  types;
- subscribe/update HTTP-200 auth envelopes and gRPC status 16 enter recovery;
- a real Set-Cookie response survives a downstream Nest failure and fresh
  manager restart;
- the full existing suite remains green for both cookie and refresh-token
  users.

## Readability Constraints

- Provider-specific logic stays in `credentials.py`.
- Session ordering and persistence stay in `session.py`.
- Call sites request a valid session and handle domain operations; they do not
  implement authentication policy.
- Comments explain ordering or compatibility constraints, not visible code.
- Retry counts and delays are named constants and injectable in tests.
- No credential values are logged.
