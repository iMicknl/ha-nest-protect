# Nest Protect authentication — limitations and options

This document records what we know about authenticating **ha-nest-protect** with Google/Nest. Read this before re-investigating OAuth or alternative APIs.

Last updated: 2026-06-17 (master token method verified in production).

## TL;DR

- **Nest Protect has no official Google API.** The Smart Device Management (SDM) OAuth integration in Home Assistant does not support Protect.
- **This integration uses the same unofficial web API** as home.nest.com (`topaz` device buckets).
- **Recommended: the Master token method.** It stores a Google **master token** (the same durable credential the mobile apps hold) and mints short-lived `nest-account` access tokens from it. It only stops working on a Google password change or explicit revocation, so HA self-refreshes indefinitely with no browser running.
- **The installed-app PKCE flow does NOT work** for the Nest OAuth client (Google returns `Access blocked / invalid_request`). See "Why PKCE is blocked" below.
- **Legacy fallback: cookie + `issueToken` auth.** Works, but **Google session cookies expire** on a server-side schedule (~2–5 hours in practice). When Google returns `USER_LOGGED_OUT`, fresh cookies from a browser are required.
- **The Chrome extension works without re-login** because Chrome still has a live Google session — it re-captures cookies, not because HA can self-heal forever.
- **Intercepting the Nest Protect device traffic does not help** (see "Packet capture" below).

---

## What this integration uses today

```mermaid
sequenceDiagram
    participant User as Browser_or_Extension
    participant Google as Google_OAuth
    participant HA as Home_Assistant
    participant Nest as Nest_Web_API

    User->>Google: Login at home.nest.com (one-time or reauth)
    User->>HA: Paste issue_token + cookies (config entry)
    HA->>Google: GET issueToken URL with stored cookies
    Google-->>HA: access_token + Set-Cookie rotations
    HA->>Nest: issue_jwt with access_token
    Nest-->>HA: Nest session (access_token, expires_in)
    HA->>Nest: subscribe_for_data (long-poll, topaz buckets)
```

Stored credentials in the config entry:

| Field | Purpose |
|-------|---------|
| `master_token` | Durable Google master token (recommended method) |
| `google_email` | Google account email, used with the master token |
| `android_id` | Generated device id, used with the master token |
| `issue_token` | Full `iframerpc?action=issueToken` URL from Google (cookie method) |
| `cookies` | Google session cookie header string (cookie method) |
| `refresh_token` | Legacy only — see below |

Runtime persistence (per config entry):

| Store | Purpose |
|-------|---------|
| `nest_protect_{entry_id}` | Nest session + transport URL for faster startup |

---

## Master token method (recommended) — durable, HA-contained

This is the durable path. It reproduces how the Nest/Google mobile apps stay logged
in: it stores a Google **master token** (`aas_et/...`) that only dies on password
change or revocation, and mints short-lived `nest-account` access tokens from it.
Everything happens inside Home Assistant — no browser extension, no companion
service, no always-on browser.

### How it works

```mermaid
sequenceDiagram
    participant User as User_Browser
    participant HA as Home_Assistant
    participant GAuth as Android_Auth_Endpoint
    participant Nest as Nest_Web_API

    User->>User: Sign in at accounts.google.com/EmbeddedSetup
    User->>HA: Paste email + one-time oauth_token cookie
    HA->>GAuth: exchange_token(oauth_token)
    GAuth-->>HA: master token (aas_et/..., durable)
    HA->>GAuth: perform_oauth(master_token, nest-account scope)
    GAuth-->>HA: access_token (ya29.*, ~1h)
    HA->>Nest: issue_jwt with access_token
    Nest-->>HA: Nest session
    Note over HA,GAuth: On expiry, HA re-runs perform_oauth from the master token forever
```

- One-time: the user signs in at `accounts.google.com/EmbeddedSetup` and copies the
  `oauth_token` cookie (`oauth2_4/...`). 2-step verification is supported.
- HA exchanges it for a durable master token via `gpsoauth` and stores
  `master_token` + `google_email` + a generated `android_id` in the config entry.
- Access tokens are minted with the Google Home app package
  (`com.google.android.apps.chromecast.app`), its signing cert, and the
  `oauth2:https://www.googleapis.com/auth/nest-account` service. This matches the
  community-validated approach for obtaining a Nest-scoped token from a master token.
- The resulting `ya29.*` token feeds the existing `authenticate` (`issue_jwt`) →
  `get_first_data` → `subscribe_for_data` pipeline unchanged.

### Why PKCE is blocked (the previous attempt)

An earlier attempt used the installed-app **PKCE** flow against the Nest iOS OAuth
client with a reversed-client-id custom-scheme redirect. Google rejects this with
**"Access blocked … doesn't comply with Google's OAuth 2.0 policy / Error 400:
invalid_request"**. Per Google's
[Oct 2023 custom-URI-scheme restrictions](https://developers.googleblog.com/2023/10/enhancing-oauth-app-impersonation-protections.html),
custom-scheme redirects are disallowed unless the OAuth client owner enables them in
advanced settings — and we don't own the Nest client. The master-token flow above
sidesteps OAuth web consent entirely (it uses the Android auth endpoint), which is
why it works.

### Code references

| Step | Location |
|------|----------|
| Master token exchange + access token mint | `pynest/client.py` → `exchange_master_token`, `get_access_token_from_master_token` |
| Constants (app pkg / cert / service) | `pynest/const.py` → `GOOGLE_HOME_APP`, `GOOGLE_OAUTH_CLIENT_SIG`, `NEST_ACCOUNT_OAUTH_SERVICE` |
| Config flow step | `config_flow.py` → `async_step_master_token` |
| Dependency | `manifest.json` → `gpsoauth` |

### When re-auth is still needed

Only when the master token is invalidated: a **Google password change** or an
**explicit revocation** in the Google account security page. There is no periodic
hours-based expiry like the cookie method.

### Confirmed working

The one open question was whether Nest's `issue_jwt` would accept a token whose OAuth
client is the Google Home app (rather than the Nest app). **It does** — this was
verified in production, running 24h+ with no deauths and no manual reauth. HA silently
mints a fresh ~1h `nest-account` access token from the stored master token as needed.

If it ever fails later, the debug log pinpoints the stage: a `BadCredentialsException`
at the token mint means the master token was revoked (e.g. password change), while a
`PynestException` at the `authenticate` step would indicate `issue_jwt` rejected the
token (app/scope params would need adjustment).

---

## Packet capture / Wireshark — ruled out

Intercepting traffic between a Nest Protect and Google cannot produce a credential
this integration can use:

- Protects talk to Google over **Weave** (802.15.4 Thread + Wi-Fi 802.11), which is
  encrypted and authenticated with **per-device hardware certificates** and is
  certificate-pinned.
- Even with full TLS/MITM decryption, you obtain *device* credentials, not a
  reusable *account* token, and the device traffic does not expose the
  `home.nest.com` account API this integration depends on.
- Therefore interception is a dead end for the auth goal. Use the Master token method.

---

## Official Google OAuth (SDM API) — not for Protect

The [official HA Nest integration](https://www.home-assistant.io/integrations/nest/) uses Google's **Smart Device Management API**:

- Proper OAuth2 with refresh tokens
- Automated token renewal
- $5 Google Device Access fee
- Pub/Sub for push updates

**Supported devices:** thermostats, cameras, doorbells, Hub Max only.

**Nest Protect is not supported** and has not been announced for SDM.

References:

- [Google supported devices](https://developers.google.com/nest/device-access/supported-devices)
- [README](../README.md) — "Google SDM doesn't support Nest Protect"

---

## Legacy OAuth `refresh_token` — dead for new setups

The codebase still consumes `refresh_token` via `NestClient.get_access_token_from_refresh_token()`,
but new ones can no longer be minted from a browser:

- Google **deprecated the browser out-of-band (OOB) OAuth flow** in October 2022
- The **installed-app PKCE flow is blocked** for the Nest client (see "Why PKCE is blocked" above)
- homebridge-nest documents the OOB removal: [issue #575](https://github.com/chrisjshull/homebridge-nest/issues/575)
- Pre-existing refresh tokens keep working until password change or revocation

For durable auth on new setups, use the **Master token method** instead.

If an `issueToken` response ever includes a `refresh_token`, the integration persists it automatically. Users who already have a legacy `refresh_token` can still use the manual config-flow path.

---

## Cookie + issueToken auth — how it works and why it expires

This is what home.nest.com uses internally. Community integrations (homebridge-nest, ha-nest-protect, nest_legacy) reverse-engineer it.

### Setup

1. User signs into Google at home.nest.com (browser or Chrome extension opens the page).
2. DevTools or the extension captures:
   - **issue_token** — request URL for `iframerpc?action=issueToken`
   - **cookies** — full `Cookie` header (homebridge recommends the `oauth2/iframe` request, not only `issueToken`)
3. HA stores both in the config entry.

### Runtime refresh (automated within HA)

- HA calls `issueToken` with stored cookies **only when the Nest session or Google access token needs renewal** (not on a fixed 15-minute timer).
- On startup, one proactive `issueToken` call refreshes cookies before subscribing.
- Google may return `Set-Cookie` headers; merged cookies, updated `issue_token` URLs, and any rare `refresh_token` values are persisted back to the config entry.
- Google access token is exchanged for a Nest session via `issue_jwt`.
- Nest session is used for `subscribe_for_data` (real-time Protect updates).

### Why auth still fails periodically

Google enforces a **server-side session lifetime** on cookie-based auth. Debug logs showed:

- Proactive `issueToken` succeeding for ~2 hours, then `USER_LOGGED_OUT`
- homebridge-nest reports similar ~2–5 hour expiry ([issue #630](https://github.com/chrisjshull/homebridge-nest/issues/630))
- Rotating 2 cookies per refresh does not prevent hard session invalidation
- HA reboot with dead cookies fails at tier-2 auth even if a persisted Nest session exists in the Store

`USER_LOGGED_OUT` means: **stored cookies are no longer valid**. HA cannot recover without new cookies from a browser (extension or manual paste).

### Why the Chrome extension does not require re-login

The extension opens home.nest.com while **Chrome still has an active Google session**. It re-captures `issue_token` + cookies from network traffic. The user is not typing credentials again — the browser session is doing the work.

HA has no browser. It only has the last saved cookie string.

See [chrome_extension/README.md](../chrome_extension/README.md).

---

## Alternative APIs for Nest Protect

| Option | Protect? | Auth | Notes |
|--------|----------|------|-------|
| **ha-nest-protect** (this repo) | Yes | Cookies + issueToken | Real-time subscriber on `topaz` buckets |
| **[nest_legacy](https://github.com/tronikos/nest_legacy)** | Yes | Same | Broader device support, same API family |
| **homebridge-nest** | Yes | Same | Same cookie ceiling |
| **Official HA Nest (SDM)** | **No** | OAuth2 | Best auth, wrong devices |
| **Works with Nest** | Was yes | API key | Deprecated 2019 |
| **Local / LAN protocol** | No | — | Protect is cloud-only |

**Switching integrations does not avoid cookie auth.** All community options hit the same undocumented API.

---

## Strategies evaluated

### Master token (recommended, implemented)

Store a durable Google master token and mint `nest-account` access tokens from it,
fully inside HA. No browser extension or companion service. Only re-auth trigger is
a Google password change / revocation. See "Master token method" above.

### HA-only cookies (legacy fallback)

Maximize uptime without an always-on browser:

- Persist rotated cookies and sync Nest session after refresh
- Capture fuller cookie sets at setup (`oauth2/iframe` header)
- Call `issueToken` only when tokens expire (avoid hammering Google)
- Keep subscriber alive when cookies die; prompt reauth once
- Survive HA reboots when cookies/session still valid

**Ceiling:** Periodic manual reauth via extension or manual paste when Google invalidates the session.

### Browser-assisted (not current scope)

Chrome extension periodically pushes fresh credentials to HA while Google remains signed in on a PC. Matches extension UX (no password re-entry). Requires always-on browser + user opt-in.

### Legacy refresh_token

Only if user already possesses a token from before 2022 deprecation.

---

## Code references

| Area | File |
|------|------|
| Config entry fields | `custom_components/nest_protect/const.py` |
| Master token mint | `custom_components/nest_protect/pynest/client.py` → `exchange_master_token`, `get_access_token_from_master_token` |
| Master token config step | `custom_components/nest_protect/config_flow.py` → `async_step_master_token` |
| Cookie auth | `custom_components/nest_protect/pynest/client.py` → `get_access_token_from_cookies` |
| Refresh token auth (legacy) | `get_access_token_from_refresh_token` |
| Session tiers + persistence | `custom_components/nest_protect/session.py` |
| Cookie persist to config | `custom_components/nest_protect/__init__.py` → `_persist_refreshed_auth` |
| Chrome extension capture | `chrome_extension/background.js` |
| Reauth flow | `custom_components/nest_protect/config_flow.py` → `async_step_reauth` |

---

## When investigating auth bugs

1. Check logs for `USER_LOGGED_OUT` vs `NotAuthenticatedException` (401 on Nest API).
2. Distinguish **dead Google cookies** (need browser reauth) from **stale Nest session** (refresh via issueToken if cookies still valid).
3. Confirm rotated cookies are persisted to the config entry after successful `issueToken`.
4. Do not assume official SDM OAuth or new refresh tokens are available for Protect.

---

## External references

- [homebridge-nest — cookies method](https://github.com/chrisjshull/homebridge-nest)
- [homebridge-nest #575 — refresh token deprecated](https://github.com/chrisjshull/homebridge-nest/issues/575)
- [homebridge-nest #630 — cookie auth expiry discussion](https://github.com/chrisjshull/homebridge-nest/issues/630)
- [Google Device Access — supported devices](https://developers.google.com/nest/device-access/supported-devices)
- [Google OAuth OOB migration](https://developers.google.com/identity/protocols/oauth2/resources/oob-migration)
