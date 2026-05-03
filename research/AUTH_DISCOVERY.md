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

| Method | Can Get New Tokens? | Longevity | Automation | Notes |
|--------|--------------------:|-----------|------------|-------|
| Cookie + issueToken (current) | Yes | Hours-days | Manual | Depends on Google session cookies |
| iOS refresh_token (legacy) | No (OOB dead) | Indefinite | Full | Only existing tokens work |
| Web client code flow | Yes | Indefinite (refresh_token) | Semi | Needs client_secret |
| GIS implicit flow (loopback) | Yes | 1 hour | Semi | Access token only, no refresh |
| PKCE with custom client | Unknown | Indefinite | Full | Requires Google Cloud project |

## Recommended Approach: Local OAuth Server

Since the web client's implicit/GIS flow returns access tokens directly (no client_secret needed), we can:

1. Start a local HTTP server on `http://localhost:<port>`
2. Open browser to Google OAuth with the web client_id + `response_type=token`
3. Receive the access_token via fragment redirect
4. Use `issue_jwt` → `/session` flow

**Problem**: The web client only has `https://home.nest.com/login/callback` as registered redirect_uri. We cannot add localhost.

**Alternative**: Use the GIS JavaScript library approach:
1. Serve a minimal HTML page locally
2. Use Google's `accounts.google.com/gsi/client` library  
3. Request tokens with `prompt: 'consent'` and `access_type: 'offline'`
4. The GIS library handles the OAuth popup/redirect internally

**Best option found**: Intercept the existing home.nest.com callback flow by navigating to the OAuth URL and capturing the code/token from the redirect. The PoC validates this approach.

## Files

- `poc_auth.py` - Proof of concept testing all auth approaches
