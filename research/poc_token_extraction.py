"""
Nest Authentication PoC - Token Extraction Method

This is the PROVEN approach for Home Assistant integration.
It works by opening home.nest.com in the user's browser and extracting the
access token from the authenticated page's gapi.auth2 instance.

How it works:
1. HA config flow opens a URL to our local server
2. Our server shows a page that opens home.nest.com and instructs the user to log in
3. After login, the user clicks "Extract Token" which runs a bookmarklet
4. The bookmarklet extracts gapi.auth2 token and redirects to our callback
5. Our server receives the token and validates the full chain

For Home Assistant integration, this maps to:
- Config flow calls async_external_step() pointing to our extraction page
- User logs in on real home.nest.com (handles all Google OAuth natively)
- After login, user clicks a button/bookmarklet that sends token to HA
- Config flow completes with valid credentials

Alternative (more seamless): After user logs in, use issueToken iframe approach
to silently refresh tokens. The initial login is the only manual step.

Usage:
    python poc_token_extraction.py

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

ISSUE_JWT_URL = "https://nestauthproxyservice-pa.googleapis.com/v1/issue_jwt"
SESSION_URL = "https://home.nest.com/session"

EXTRACTION_PAGE_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>Nest Protect - Connect Account</title>
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
            max-width: 500px;
            width: 90%;
        }
        h2 { color: #333; margin-bottom: 8px; }
        p { color: #666; line-height: 1.6; }
        .steps { text-align: left; margin: 20px 0; }
        .steps li { margin: 12px 0; color: #444; }
        .btn {
            display: inline-block;
            padding: 14px 36px;
            background: #4285f4;
            color: white;
            border: none;
            border-radius: 6px;
            font-size: 16px;
            cursor: pointer;
            text-decoration: none;
            transition: background 0.2s;
            margin: 8px;
        }
        .btn:hover { background: #3367d6; }
        .btn.secondary { background: #34a853; }
        .btn.secondary:hover { background: #2d9249; }
        .btn:disabled { background: #ccc; cursor: not-allowed; }
        .status { margin-top: 20px; padding: 12px; border-radius: 6px; display: none; }
        .status.show { display: block; }
        .status.success { background: #e8f5e9; color: #2e7d32; }
        .status.error { background: #fbe9e7; color: #c62828; }
        .status.loading { background: #e3f2fd; color: #1565c0; }
        code {
            background: #f5f5f5;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 13px;
        }
        .divider { border-top: 1px solid #eee; margin: 24px 0; }
    </style>
</head>
<body>
<div class="container">
    <h2>Nest Protect</h2>
    <p>Connect your Nest account to Home Assistant</p>

    <ol class="steps">
        <li><strong>Step 1:</strong> Click below to open Nest. Sign in if needed.</li>
        <li><strong>Step 2:</strong> Once you see your Nest home, come back here.</li>
        <li><strong>Step 3:</strong> Click "Extract Token" to complete setup.</li>
    </ol>

    <a class="btn" href="https://home.nest.com" target="_blank" id="open-nest">
        Open Nest Login
    </a>

    <div class="divider"></div>

    <p><strong>After signing in to Nest, click below:</strong></p>
    <button class="btn secondary" id="extract-btn" onclick="extractToken()">
        Extract Token
    </button>

    <div id="status" class="status"></div>
</div>

<script>
const CALLBACK_URL = '""" + BASE_URL + """/auth/nest/callback';

function extractToken() {
    showStatus('Opening Nest page to extract token...', 'loading');
    document.getElementById('extract-btn').disabled = true;

    // Open home.nest.com and try to extract the token
    // We use a popup and wait for it to send us the token via postMessage
    var popup = window.open('https://home.nest.com', '_blank', 'width=800,height=600');

    // Listen for postMessage from our injected script
    window.addEventListener('message', function handler(event) {
        if (event.data && event.data.type === 'nest_token') {
            window.removeEventListener('message', handler);
            if (popup) popup.close();

            if (event.data.access_token) {
                showStatus('Got token! Validating...', 'loading');
                sendToken(event.data.access_token);
            } else {
                showStatus('Could not extract token. Make sure you are logged in to Nest.', 'error');
                document.getElementById('extract-btn').disabled = false;
            }
        }
    });

    // Alternative: poll the popup to check if it's logged in
    // (postMessage won't work cross-origin, so we use the manual approach below)
    setTimeout(function() {
        showStatus(
            'Cross-origin restriction: Please use the manual method below. ' +
            'Open browser console on home.nest.com and paste the extraction script.',
            'error'
        );
        document.getElementById('extract-btn').disabled = false;
        document.getElementById('manual-section').style.display = 'block';
    }, 3000);
}

function sendToken(token) {
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
            document.getElementById('extract-btn').disabled = false;
        }
    })
    .catch(err => {
        showStatus('Error: ' + err.message, 'error');
        document.getElementById('extract-btn').disabled = false;
    });
}

function showStatus(msg, type) {
    var el = document.getElementById('status');
    el.className = 'status show ' + type;
    el.textContent = msg;
}

// Also accept token via URL parameter (for bookmarklet redirect)
(function() {
    var params = new URLSearchParams(window.location.search);
    var token = params.get('token');
    if (token) {
        showStatus('Received token! Validating...', 'loading');
        sendToken(token);
    }
})();
</script>

<!-- Manual extraction section (shown if popup method fails) -->
<div id="manual-section" style="display:none; position:fixed; bottom:0; left:0; right:0; background:white; padding:20px; box-shadow:0 -2px 10px rgba(0,0,0,0.1); text-align:left; max-height:40vh; overflow:auto;">
    <h3 style="margin-top:0;">Manual Token Extraction</h3>
    <p>On the <strong>home.nest.com</strong> tab, open browser DevTools (F12) → Console, and paste:</p>
    <pre style="background:#1e1e1e; color:#d4d4d4; padding:12px; border-radius:6px; overflow-x:auto; font-size:12px;">
// Extract and send Nest token to Home Assistant
(function() {
    var auth = gapi.auth2.getAuthInstance();
    var user = auth.currentUser.get();
    var resp = user.getAuthResponse(true);
    if (resp && resp.access_token) {
        window.location = '""" + BASE_URL + """/auth/nest/callback?token=' + encodeURIComponent(resp.access_token);
    } else {
        alert('No token found. Please sign in first.');
    }
})();
    </pre>
    <p style="color:#666; font-size:13px;">Or drag this to your bookmarks bar:
    <a href="javascript:void((function(){var a=gapi.auth2.getAuthInstance().currentUser.get().getAuthResponse(true);if(a&&a.access_token){window.location='""" + BASE_URL + """/auth/nest/callback?token='+encodeURIComponent(a.access_token)}else{alert('Not logged in')}})())" style="background:#4285f4; color:white; padding:4px 12px; border-radius:4px; text-decoration:none; font-size:12px;">Send Nest Token to HA</a>
    </p>
</div>

</body>
</html>"""


class NestTokenServer:
    """Server that extracts tokens from a logged-in Nest session."""

    def __init__(self):
        self.captured_token = None
        self.session_data = None
        self.token_event = asyncio.Event()

    async def start(self):
        app = web.Application()
        app.router.add_get("/auth/nest/start", self.handle_start)
        app.router.add_post("/auth/nest/callback", self.handle_callback_post)
        app.router.add_get("/auth/nest/callback", self.handle_callback_get)
        app.router.add_get("/auth/nest/status", self.handle_status)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, PROXY_HOST, PROXY_PORT)
        await site.start()
        return runner

    async def handle_start(self, request: web.Request) -> web.Response:
        """Serve the token extraction page."""
        return web.Response(text=EXTRACTION_PAGE_HTML, content_type="text/html")

    async def handle_callback_post(self, request: web.Request) -> web.Response:
        """Receive token via POST (from fetch)."""
        try:
            data = await request.json()
            access_token = data.get("access_token")
            return await self._process_token(access_token)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)})

    async def handle_callback_get(self, request: web.Request) -> web.Response:
        """Receive token via GET (from bookmarklet redirect)."""
        access_token = request.query.get("token")
        if not access_token:
            return web.Response(text="No token provided", status=400)

        result = await self._process_token(access_token)
        data = json.loads(result.text)
        if data.get("success"):
            return web.Response(
                text="<html><body><h2>Success!</h2>"
                     "<p>Authentication complete. You can close this window and return to Home Assistant.</p>"
                     f"<p>Email: {self.session_data.get('email', 'unknown')}</p>"
                     "</body></html>",
                content_type="text/html",
            )
        else:
            return web.Response(
                text=f"<html><body><h2>Failed</h2><p>{data.get('error')}</p></body></html>",
                content_type="text/html",
            )

    async def _process_token(self, access_token: Optional[str]) -> web.Response:
        """Validate token and store session."""
        if not access_token:
            return web.json_response({"success": False, "error": "no token"})

        print(f"\n  Received access token: {access_token[:40]}...")

        jwt = self._issue_jwt(access_token)
        if not jwt:
            return web.json_response({"success": False, "error": "issue_jwt failed"})

        session = self._get_session(jwt)
        if not session:
            return web.json_response({"success": False, "error": "session failed"})

        self.captured_token = access_token
        self.session_data = session
        self.token_event.set()

        print(f"  Validated! User: {session.get('email')}")
        return web.json_response({"success": True})

    async def handle_status(self, request: web.Request) -> web.Response:
        """Check auth status."""
        if self.captured_token:
            return web.json_response({
                "authenticated": True,
                "email": self.session_data.get("email") if self.session_data else None,
            })
        return web.json_response({"authenticated": False})

    def _issue_jwt(self, access_token: str) -> Optional[str]:
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

    def _get_session(self, jwt: str) -> Optional[dict]:
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
    print("  Nest Auth PoC - Token Extraction Method")
    print("=" * 60)
    print()
    print("  This demonstrates the practical approach for HA:")
    print("  1. User logs in to real home.nest.com")
    print("  2. Token is extracted from the authenticated page")
    print("  3. Token is sent to HA via local callback")
    print()
    print("  For Home Assistant integration:")
    print("  - Config flow opens this URL via async_external_step()")
    print("  - User completes login on home.nest.com")
    print("  - Token sent back to HA, stored for refresh")

    server = NestTokenServer()
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
        print(f"\n  This validates the full auth chain works.")
        print(f"  In HA, the integration would store this and use")
        print(f"  issue_jwt periodically for fresh sessions.")

    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
