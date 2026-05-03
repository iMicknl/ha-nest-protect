"""
Nest Authentication PoC - OAuth Page Method (Recommended)

This is the SIMPLEST and most reliable approach for Home Assistant users.
Instead of complex proxying, we serve a minimal HTML page that:
1. Loads Google's Sign-In JavaScript library (gapi.auth2)
2. User clicks "Sign in with Google" → Google popup handles auth
3. Google returns access_token directly to our page via postMessage
4. Our page sends the token back to HA via a local callback

This avoids all the redirect_uri problems because:
- Google's GIS implicit flow doesn't need a registered redirect_uri
- The token is delivered via JavaScript postMessage, not HTTP redirect
- We use Google's own client_id (which has nest-account scope approved)

For Home Assistant integration, this would be:
- Config flow calls async_external_step() with URL to our auth page
- The auth page is served by a HomeAssistantView
- After token capture, page redirects to HA callback URL
- Config flow receives the token and completes setup

Usage:
    python poc_oauth_page.py

Then open http://localhost:8123/auth/nest/start in your browser.

Requirements:
    pip install aiohttp
"""

import asyncio
import json
import urllib.request
import urllib.error
from typing import Optional
from aiohttp import web


PROXY_HOST = "localhost"
PROXY_PORT = 9876
BASE_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"

WEB_CLIENT_ID = "733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com"
ISSUE_JWT_URL = "https://nestauthproxyservice-pa.googleapis.com/v1/issue_jwt"
SESSION_URL = "https://home.nest.com/session"

AUTH_PAGE_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>Nest - Sign in with Google</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            background: #f5f5f5;
        }
        .container {
            background: white;
            padding: 40px;
            border-radius: 12px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            text-align: center;
            max-width: 400px;
            width: 90%;
        }
        h2 { color: #333; margin-bottom: 8px; }
        p { color: #666; margin-bottom: 24px; }
        .status { margin-top: 20px; padding: 12px; border-radius: 6px; }
        .status.success { background: #e8f5e9; color: #2e7d32; }
        .status.error { background: #fbe9e7; color: #c62828; }
        .status.loading { background: #e3f2fd; color: #1565c0; }
        #signin-btn {
            display: inline-block;
            padding: 12px 32px;
            background: #4285f4;
            color: white;
            border: none;
            border-radius: 4px;
            font-size: 16px;
            cursor: pointer;
            transition: background 0.2s;
        }
        #signin-btn:hover { background: #3367d6; }
        #signin-btn:disabled { background: #ccc; cursor: not-allowed; }
    </style>
</head>
<body>
<div class="container">
    <h2>Nest Protect</h2>
    <p>Sign in with your Google account to connect your Nest devices.</p>

    <button id="signin-btn" onclick="doSignIn()" disabled>Loading...</button>
    <div id="status"></div>
</div>

<script src="https://apis.google.com/js/api.js"></script>
<script>
const CLIENT_ID = '""" + WEB_CLIENT_ID + """';
const SCOPES = 'email profile openid https://www.googleapis.com/auth/nest-account';
const CALLBACK_URL = '""" + BASE_URL + """/auth/nest/callback';

let auth2;

// Initialize gapi.auth2
gapi.load('auth2', function() {
    auth2 = gapi.auth2.init({
        client_id: CLIENT_ID,
        scope: SCOPES,
        ux_mode: 'popup',
    });

    auth2.then(function() {
        document.getElementById('signin-btn').disabled = false;
        document.getElementById('signin-btn').textContent = 'Sign in with Google';

        // Check if already signed in
        if (auth2.isSignedIn.get()) {
            let user = auth2.currentUser.get();
            let authResp = user.getAuthResponse(true);
            if (authResp && authResp.access_token) {
                showStatus('Already signed in! Sending token...', 'loading');
                sendToken(authResp.access_token);
                return;
            }
        }
    }, function(error) {
        showStatus('Failed to initialize: ' + JSON.stringify(error), 'error');
    });
});

function doSignIn() {
    document.getElementById('signin-btn').disabled = true;
    showStatus('Opening Google sign-in...', 'loading');

    // Use gapi.auth2.authorize for more control (doesn't require gapi.auth2.init match)
    gapi.auth2.authorize({
        client_id: CLIENT_ID,
        scope: SCOPES,
        response_type: 'token',
        prompt: 'select_account',
    }, function(response) {
        if (response.error) {
            showStatus('Sign-in failed: ' + response.error, 'error');
            document.getElementById('signin-btn').disabled = false;
            return;
        }

        if (response.access_token) {
            showStatus('Got token! Validating...', 'loading');
            sendToken(response.access_token);
        } else {
            showStatus('No access token in response', 'error');
            document.getElementById('signin-btn').disabled = false;
        }
    });
}

function sendToken(token) {
    // Send the token to our callback endpoint
    fetch(CALLBACK_URL, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({access_token: token})
    })
    .then(resp => resp.json())
    .then(data => {
        if (data.success) {
            showStatus('Authentication successful! You can close this window.', 'success');
        } else {
            showStatus('Validation failed: ' + (data.error || 'unknown'), 'error');
            document.getElementById('signin-btn').disabled = false;
        }
    })
    .catch(err => {
        showStatus('Error sending token: ' + err.message, 'error');
        document.getElementById('signin-btn').disabled = false;
    });
}

function showStatus(msg, type) {
    let el = document.getElementById('status');
    el.className = 'status ' + type;
    el.textContent = msg;
}
</script>
</body>
</html>"""


class NestAuthServer:
    """Simple auth server that serves a Google Sign-In page and captures the token."""

    def __init__(self):
        self.captured_token = None
        self.session_data = None
        self.token_event = asyncio.Event()

    async def start(self):
        app = web.Application()
        app.router.add_get("/auth/nest/start", self.handle_start)
        app.router.add_post("/auth/nest/callback", self.handle_callback)
        app.router.add_get("/auth/nest/status", self.handle_status)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, PROXY_HOST, PROXY_PORT)
        await site.start()
        return runner

    async def handle_start(self, request: web.Request) -> web.Response:
        """Serve the Google Sign-In page."""
        return web.Response(text=AUTH_PAGE_HTML, content_type="text/html")

    async def handle_callback(self, request: web.Request) -> web.Response:
        """Receive the access token from the sign-in page."""
        try:
            data = await request.json()
            access_token = data.get("access_token")

            if not access_token:
                return web.json_response({"success": False, "error": "no token"})

            # Validate the full chain
            print(f"\n  Received access token: {access_token[:40]}...")

            # Step 1: issue_jwt
            jwt = await self._issue_jwt(access_token)
            if not jwt:
                return web.json_response({"success": False, "error": "issue_jwt failed"})

            # Step 2: create session
            session = await self._get_session(jwt)
            if not session:
                return web.json_response({"success": False, "error": "session failed"})

            self.captured_token = access_token
            self.session_data = session
            self.token_event.set()

            print(f"  Validated! User: {session.get('email')}")
            return web.json_response({"success": True})

        except Exception as e:
            return web.json_response({"success": False, "error": str(e)})

    async def handle_status(self, request: web.Request) -> web.Response:
        """Check if auth has completed (for polling from HA config flow)."""
        if self.captured_token:
            return web.json_response({
                "authenticated": True,
                "email": self.session_data.get("email") if self.session_data else None,
            })
        return web.json_response({"authenticated": False})

    async def _issue_jwt(self, access_token: str) -> Optional[str]:
        """Exchange Google access token for Nest JWT."""
        payload = json.dumps({
            "policy_id": "authproxy-oauth-policy",
            "google_oauth_access_token": access_token,
            "embed_google_oauth_access_token": True,
            "expire_after": "3600s",
        }).encode()

        req = urllib.request.Request(ISSUE_JWT_URL, data=payload, headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        })

        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
                jwt = data.get("jwt")
                print(f"  JWT: {jwt[:50]}...")
                return jwt
        except urllib.error.HTTPError as e:
            print(f"  issue_jwt failed: {e.code} - {e.read().decode()[:100]}")
            return None

    async def _get_session(self, jwt: str) -> Optional[dict]:
        """Create Nest session from JWT."""
        req = urllib.request.Request(SESSION_URL, headers={
            "Authorization": f"Basic {jwt}",
            "X-Requested-With": "XMLHttpRequest",
        })

        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f"  session failed: {e.code} - {e.read().decode()[:100]}")
            return None


async def main():
    print("=" * 60)
    print("  Nest Auth PoC - OAuth Page Method")
    print("=" * 60)
    print()
    print("  This serves a Google Sign-In page that captures the")
    print("  access token client-side, then validates the full chain.")
    print()
    print("  How this maps to Home Assistant:")
    print("  - Config flow uses async_external_step() with this URL")
    print("  - HomeAssistantView serves the sign-in page")
    print("  - After auth, config flow receives token via callback")
    print("  - Integration stores token + uses issue_jwt for refresh")

    server = NestAuthServer()
    runner = await server.start()

    login_url = f"{BASE_URL}/auth/nest/start"
    print(f"\n  {'─' * 50}")
    print(f"  Open this URL in your browser:")
    print(f"  → {login_url}")
    print(f"  {'─' * 50}")
    print(f"\n  Waiting for authentication (5 min timeout)...\n")

    try:
        await asyncio.wait_for(server.token_event.wait(), timeout=300)
    except asyncio.TimeoutError:
        print("\n  Timeout - no token received.")
        await runner.cleanup()
        return

    if server.session_data:
        print(f"\n{'=' * 60}")
        print(f"  SUCCESS!")
        print(f"{'=' * 60}")
        print(f"  Email:     {server.session_data.get('email')}")
        print(f"  UserID:    {server.session_data.get('userid')}")
        print(f"  Transport: {server.session_data.get('urls', {}).get('transport_url', '')}")
        print(f"\n  Token: {server.captured_token[:50]}...")
        print(f"\n  This token is valid for ~1 hour.")
        print(f"  In HA, the integration would:")
        print(f"  1. Store the Google session cookies (via issueToken iframe)")
        print(f"  2. Periodically refresh the token using gapi.auth2")
        print(f"  3. Use issue_jwt + /session for each API call")

    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
