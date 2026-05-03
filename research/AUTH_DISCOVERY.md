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

### REJECTED: Google Login Proxy (Cookie Capture)

Attempted: Proxy `accounts.google.com` login page to capture Set-Cookie headers during authentication.

**Result**: The page loads (1.2MB SPA) but the internal XHR calls for email validation and password submission don't execute properly through the proxy. Google's login SPA has integrity checks and dynamic endpoint calls that break under URL rewriting.

## Recommended Approach: Guided Setup with Cookie-Based Refresh

**Proven working** - full chain validated end-to-end on 2026-05-03.

### Key Discovery: issueToken + Minimal Cookies

The `issueToken` endpoint at `accounts.google.com/o/oauth2/iframerpc` can generate fresh access tokens given:
1. **4 Google session cookies** (last 1+ year)
2. **An opaque `login_hint`** (stable per user, extracted from `gapi.auth2`)

Minimal required cookies (all from `accounts.google.com`):
| Cookie | HttpOnly | Expires |
|--------|----------|---------|
| `SID` | No | ~1 year |
| `LSID` | Yes | ~1 year |
| `__Secure-1PSIDTS` | Yes | ~1 year |
| `__Secure-3PSID` | Yes | ~1 year |

### Setup Flow for HA Users

```
Step 1: Token Extraction (automated via bookmarklet)
  - User opens home.nest.com and signs in
  - Bookmarklet extracts: access_token + login_hint + email
  - Sent to HA callback, validates full chain
  
Step 2: Cookie Provision (semi-manual, one-time)
  - User opens DevTools on accounts.google.com → Cookies
  - Copies 4 specific cookie values into HA's setup page
  - HA validates by calling issueToken with cookies + login_hint
  
Result: HA stores issue_token_url + cookies
  - Automated refresh for 1+ year
  - Uses existing NestClient.get_access_token_from_cookies()
```

### Bookmarklet (runs on home.nest.com)

```javascript
(function() {
    var a = gapi.auth2.getAuthInstance().currentUser.get();
    var r = a.getAuthResponse(true);
    window.location = 'http://HA_URL/callback?data=' + 
        encodeURIComponent(JSON.stringify({
            access_token: r.access_token,
            login_hint: r.login_hint,
            email: a.getBasicProfile().getEmail()
        }));
})();
```

### issue_token URL Construction

```
https://accounts.google.com/o/oauth2/iframerpc
  ?action=issueToken
  &response_type=token id_token
  &login_hint=<opaque_login_hint>
  &client_id=733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com
  &origin=https://home.nest.com
  &scope=openid profile email https://www.googleapis.com/auth/nest-account
  &ss_domain=https://home.nest.com
```

### What This Improves Over Current Setup

| Aspect | Current | Improved |
|--------|---------|----------|
| issue_token URL | User finds in Network tab | Auto-constructed from login_hint |
| Cookies | User finds in request headers | Guided: "paste these 4 values" |
| Validation | None during setup | Validates full chain before saving |
| Instructions | Generic wiki page | Interactive setup page in HA |

### Token Refresh Chain (same as current, now automated setup)

```
Stored: issue_token_url + cookies
  ↓ (every ~55 minutes)
issueToken(cookies, login_hint) → Google access_token (1hr)
  ↓
issue_jwt(access_token) → Nest JWT (1hr)
  ↓  
/session(jwt) → transport_url + session (30 days metadata)
  ↓
Transport API calls with JWT auth
```

## Files

- `poc_auth.py` - Original PoC testing all auth approaches (CDP-based)
- `poc_oauth_page.py` - REJECTED: Local OAuth page approach (origin check fails)
- `poc_proxy_auth.py` - REJECTED: Reverse proxy approach (origin check fails)
- `poc_cookie_proxy.py` - REJECTED: Google login proxy (SPA too complex to proxy)
- `poc_token_extraction.py` - Token extraction from home.nest.com (proven working)
- `poc_guided_setup.py` - **RECOMMENDED**: Complete guided setup with cookie refresh (proven working)
