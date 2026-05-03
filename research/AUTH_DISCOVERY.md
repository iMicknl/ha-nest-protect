# Nest Authentication Flow Discovery

Research conducted 2026-05-03 by tracing the live `home.nest.com` login flow via Chrome DevTools Protocol.

## Verified Auth Flow (Browser)

```
User clicks "Sign in with Google"
    → Google Account Chooser
        client_id: 733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com (WEB)
        scope: email profile openid https://www.googleapis.com/auth/nest-account
        response_type: permission id_token
        redirect_uri: https://home.nest.com/login/callback
        gsiwebsdk: 2
    → Google Consent / Interactive Login
    → Redirect to home.nest.com/login/callback
    → GIS SDK (Google Identity Services v2) delivers access_token via postMessage
    → POST nestauthproxyservice-pa.googleapis.com/v1/issue_jwt
        Headers: Authorization: Bearer <google_access_token>
        Body: {
            "policy_id": "authproxy-oauth-policy",
            "google_oauth_access_token": "<ya29...>",
            "embed_google_oauth_access_token": true,
            "expire_after": "3600s"
        }
        Response: {"jwt": "g.0.eyJ..."}
    → GET home.nest.com/session
        Headers: Authorization: Basic <jwt>
        Response: {userid, access_token, transport_url, email, expires_in, ...}
    → App operational
```

## Key Discoveries

### 1. Two Client IDs Exist

| Client ID | Type | Status |
|-----------|------|--------|
| `733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq` | Web (confidential) | **Active** - used by home.nest.com |
| `733249279899-1gpkq9duqmdp55a7e5lft1pr2smumdla` | iOS (public/native) | **Dead** - OOB deprecated, loopback rejected |

### 2. The iOS Client ID is Completely Dead

Tested all redirect options for the iOS client:
- `urn:ietf:wg:oauth:2.0:oob` → Error 400: invalid_request (OOB deprecated Jan 2023)
- `http://localhost:8888` → Error 400: invalid_request (not registered)
- `com.nestlabs.jasper://oauth2callback` → Error 400: invalid_request (custom scheme rejected)

**Implication**: The `refresh_token` flow in our current code can only work with EXISTING tokens obtained before Jan 2023. No new refresh tokens can be obtained via the iOS client.

### 3. The Web Client Accepts Authorization Code Flow

Successfully tested:
```
GET https://accounts.google.com/o/oauth2/auth
    ?client_id=733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com
    &redirect_uri=https://home.nest.com/login/callback
    &response_type=code
    &scope=openid+profile+email+https://www.googleapis.com/auth/nest-account
    &access_type=offline
    &prompt=consent
```

This returns an authorization code (e.g., `4/0AeoWuM9...`) BUT exchanging it requires a `client_secret` which is not exposed client-side (handled server-side by `home.nest.com/login/callback`).

### 4. Silent Token Refresh via OAuth Iframe

The browser silently refreshes tokens via:
1. `accounts.google.com/o/oauth2/iframe` loaded in hidden iframe
2. `gapi.auth2.getAuthInstance().currentUser.get().reloadAuthResponse()` 
3. Returns new access_token (3600s expiry) with **zero visible network calls**
4. Works as long as Google session cookies are valid

### 5. Minimal Token Flow Verified (Pure Python)

Confirmed that the entire auth chain works from plain Python (no browser/cookies needed):
```python
access_token → issue_jwt → /session → fully operational
```

The ONLY challenge is obtaining that initial Google access_token with `nest-account` scope.

## Auth Method Comparison

| Method | Can Get New Tokens? | Longevity | Automation | HA Compatible | Notes |
|--------|--------------------:|-----------|------------|:---:|-------|
| Cookie + issueToken (current) | Yes | Hours-days | Manual | Yes | Depends on Google session cookies |
| iOS refresh_token (legacy) | No (OOB dead) | Indefinite | Full | Yes | Only existing tokens work |
| Web client code flow | Yes | Indefinite (refresh_token) | Semi | No | Needs client_secret (server-side) |
| GIS implicit flow (loopback) | No | N/A | N/A | No | **REJECTED**: origin must be registered |
| Reverse proxy of home.nest.com | No | N/A | N/A | No | **REJECTED**: origin check still fails |
| Token extraction from home.nest.com | Yes | 1 hour | Semi | **Yes** | **RECOMMENDED** - proven working |
| PKCE with custom client | No | N/A | N/A | No | nest-account scope is first-party only |

## Approaches Tested and Results

### REJECTED: Local OAuth Page (gapi.auth2)

Attempted: Serve a local HTML page using Google's `gapi.auth2.authorize()` with the Nest web client_id.

**Result**: `idpiframe_initialization_failed` - "Not a valid origin for the client: http://localhost:9876 has not been registered for client ID 733249279899-..."

Google OAuth requires the JavaScript origin to be registered in the client's console project. The Nest client only allows `https://home.nest.com`. There is no way to add localhost or any other origin.

### REJECTED: Reverse Proxy (AuthCaptureProxy pattern)

Attempted: Proxy `home.nest.com` through a local server and intercept the `issue_jwt` call.

**Result**: Even though we successfully proxied the page content and rewrote URLs, the OAuth iframe still checks the browser's actual origin (`http://localhost:9876`), not the proxied target. Google rejects the OAuth initialization with the same origin error.

### REJECTED: PKCE with Custom OAuth Client

The `nest-account` scope is restricted to Google's own first-party applications. A custom Google Cloud project cannot request this scope, so creating our own OAuth client is not possible.

## Recommended Approach: Token Extraction

**Proven working** - validated end-to-end on 2026-05-03.

The approach:
1. User opens `home.nest.com` and signs in (or is already signed in)
2. The authenticated page has `gapi.auth2.getAuthInstance()` with a valid access token
3. A bookmarklet/console script extracts the token: `gapi.auth2.getAuthInstance().currentUser.get().getAuthResponse(true).access_token`
4. Token is sent to HA's local callback via redirect/fetch
5. HA validates: `access_token` → `issue_jwt` → `/session`

For Home Assistant integration:
- Config flow uses `async_external_step()` to open an instruction page
- Instruction page tells user to log in to `home.nest.com`
- After login, user uses bookmarklet or console paste to send token
- Config flow receives token, validates, stores credentials
- Token refresh uses `issue_jwt` (token valid 1 hour, Nest session longer)

### Token Refresh Strategy

After initial authentication:
1. Store the Google access token (1 hour validity)
2. Use `issue_jwt` to get a JWT (also ~1 hour)
3. Use `/session` to get transport_url + session token
4. The Nest session itself lasts longer than the access token
5. For re-auth: user must repeat the extraction process

### Improving UX: Auto-extraction via Service Worker or Extension

For better UX, future work could explore:
- A companion browser extension that auto-extracts tokens
- A PWA/Service Worker on `home.nest.com` (not feasible due to same-origin)
- The `issueToken` iframe approach (current method) as a fallback

## Files

- `poc_auth.py` - Original PoC testing all auth approaches (CDP-based)
- `poc_oauth_page.py` - REJECTED: Local OAuth page approach (origin check fails)
- `poc_proxy_auth.py` - REJECTED: Reverse proxy approach (origin check fails)
- `poc_token_extraction.py` - **RECOMMENDED**: Token extraction from home.nest.com (proven working)

## Files

- `poc_auth.py` - Proof of concept testing all auth approaches
