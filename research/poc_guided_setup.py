"""
Nest Authentication PoC - Guided Setup (Definitive Approach)

This is the COMPLETE, PROVEN approach for Home Assistant integration.
Combines the best of all research into a single setup flow:

1. INITIAL AUTH: Bookmarklet on home.nest.com extracts access_token + login_hint
2. LONG-TERM REFRESH: User provides 4 Google cookies (guided, one-time)
3. VALIDATION: Full chain verified before completing setup

Credentials stored:
- issue_token URL (constructed from login_hint) → stable
- Google cookies (SID, LSID, __Secure-1PSIDTS, __Secure-3PSID) → 1+ year lifetime
- These enable automated token refresh without user interaction

For Home Assistant:
- Config flow uses async_external_step() to open our setup page
- Page guides user through 2 steps: bookmarklet + cookie paste
- On completion, config flow stores credentials
- NestClient uses stored credentials for automated refresh (1+ year)

Usage:
    python poc_guided_setup.py

Then open http://localhost:9876/ in your browser and follow instructions.

Requirements:
    pip install aiohttp
"""

import asyncio
import json
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional
from aiohttp import web


PROXY_HOST = "localhost"
PROXY_PORT = 9876
BASE_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"

WEB_CLIENT_ID = "733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com"
ISSUE_TOKEN_BASE = "https://accounts.google.com/o/oauth2/iframerpc"
ISSUE_JWT_URL = "https://nestauthproxyservice-pa.googleapis.com/v1/issue_jwt"
SESSION_URL = "https://home.nest.com/session"

# The bookmarklet that runs on home.nest.com to extract credentials
BOOKMARKLET_CODE = """
(function() {
    try {
        var auth = gapi.auth2.getAuthInstance();
        var user = auth.currentUser.get();
        var resp = user.getAuthResponse(true);
        if (!resp || !resp.access_token) {
            alert('Not logged in to Nest. Please sign in first.');
            return;
        }
        var data = {
            access_token: resp.access_token,
            login_hint: resp.login_hint,
            email: user.getBasicProfile().getEmail()
        };
        window.location = '__CALLBACK_URL__?data=' + encodeURIComponent(JSON.stringify(data));
    } catch(e) {
        alert('Error: ' + e.message + '. Make sure you are on home.nest.com and logged in.');
    }
})();
""".strip()

SETUP_PAGE_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>Nest Protect - Setup</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0; padding: 20px;
            background: #f5f5f5;
            color: #333;
        }
        .container {
            max-width: 600px;
            margin: 0 auto;
        }
        .card {
            background: white;
            padding: 24px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            margin-bottom: 16px;
        }
        h1 { font-size: 24px; margin: 0 0 8px; }
        h2 { font-size: 18px; margin: 0 0 12px; color: #1a73e8; }
        p { line-height: 1.6; color: #555; margin: 8px 0; }
        .step-num {
            display: inline-block;
            width: 28px; height: 28px;
            background: #1a73e8;
            color: white;
            border-radius: 50%;
            text-align: center;
            line-height: 28px;
            font-weight: bold;
            font-size: 14px;
            margin-right: 8px;
        }
        .btn {
            display: inline-block;
            padding: 10px 24px;
            background: #1a73e8;
            color: white;
            border: none;
            border-radius: 6px;
            font-size: 14px;
            cursor: pointer;
            text-decoration: none;
            margin: 4px;
        }
        .btn:hover { background: #1557b0; }
        .btn.green { background: #34a853; }
        .btn.green:hover { background: #2d9249; }
        .btn.outline {
            background: white;
            color: #1a73e8;
            border: 2px solid #1a73e8;
        }
        .btn:disabled { background: #ccc; cursor: not-allowed; }
        .bookmarklet-link {
            display: inline-block;
            padding: 8px 16px;
            background: #fbbc04;
            color: #333;
            border-radius: 4px;
            text-decoration: none;
            font-weight: bold;
            cursor: grab;
            border: 2px dashed #f9a825;
        }
        .bookmarklet-link:hover { background: #f9a825; }
        input[type="text"], textarea {
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 6px;
            font-family: monospace;
            font-size: 13px;
            margin: 4px 0;
        }
        textarea { height: 60px; resize: vertical; }
        .cookie-input { margin: 8px 0; }
        .cookie-input label {
            display: block;
            font-size: 13px;
            font-weight: bold;
            color: #555;
            margin-bottom: 2px;
        }
        .status {
            padding: 12px;
            border-radius: 6px;
            margin: 12px 0;
            display: none;
        }
        .status.show { display: block; }
        .status.success { background: #e8f5e9; color: #2e7d32; }
        .status.error { background: #fbe9e7; color: #c62828; }
        .status.info { background: #e3f2fd; color: #1565c0; }
        .hidden { display: none; }
        code {
            background: #f5f5f5;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 12px;
        }
        .instructions {
            background: #f8f9fa;
            padding: 12px;
            border-radius: 6px;
            border-left: 4px solid #1a73e8;
            margin: 12px 0;
            font-size: 13px;
        }
        .instructions ol { margin: 8px 0; padding-left: 20px; }
        .instructions li { margin: 4px 0; }
        .check { color: #34a853; font-weight: bold; }
        .divider { border-top: 1px solid #eee; margin: 20px 0; }
    </style>
</head>
<body>
<div class="container">
    <div class="card">
        <h1>Nest Protect Setup</h1>
        <p>Connect your Nest account to Home Assistant. This takes about 2 minutes.</p>
    </div>

    <!-- STEP 1: Extract token + login_hint -->
    <div class="card" id="step1-card">
        <h2><span class="step-num">1</span> Connect Your Nest Account</h2>
        <p>First, open Nest and sign in with your Google account:</p>
        <a class="btn" href="https://home.nest.com" target="_blank">Open home.nest.com</a>

        <div class="divider"></div>

        <p>Once you're signed in and see your Nest home, use one of these methods:</p>

        <p><strong>Method A:</strong> Drag this to your bookmarks bar, then click it on the Nest page:</p>
        <p><a class="bookmarklet-link" href="__BOOKMARKLET__" title="Drag to bookmarks bar">Send Nest Token to HA</a></p>

        <p style="margin-top:16px"><strong>Method B:</strong> Open DevTools Console (F12) on home.nest.com and paste:</p>
        <div class="instructions">
            <code id="console-script">__CONSOLE_SCRIPT__</code>
        </div>

        <div id="step1-status" class="status"></div>
    </div>

    <!-- STEP 2: Provide cookies for long-term refresh -->
    <div class="card hidden" id="step2-card">
        <h2><span class="step-num">2</span> Enable Long-Term Refresh</h2>
        <p id="step2-greeting"></p>
        <p>For automatic token refresh (so you don't have to re-authenticate), provide your Google session cookies.</p>
        <p>These cookies last <strong>1+ year</strong> and enable HA to refresh tokens automatically.</p>

        <div class="instructions">
            <strong>How to get cookies:</strong>
            <ol>
                <li>Open <a href="https://accounts.google.com" target="_blank">accounts.google.com</a> in your browser</li>
                <li>Open DevTools (F12) → Application tab → Cookies → accounts.google.com</li>
                <li>Find and copy the values for these 4 cookies:</li>
            </ol>
        </div>

        <div class="cookie-input">
            <label>SID</label>
            <input type="text" id="cookie-SID" placeholder="Paste SID cookie value here">
        </div>
        <div class="cookie-input">
            <label>__Secure-3PSID</label>
            <input type="text" id="cookie-__Secure-3PSID" placeholder="Paste __Secure-3PSID cookie value here">
        </div>
        <div class="cookie-input">
            <label>LSID (from accounts.google.com, NOT .google.com)</label>
            <input type="text" id="cookie-LSID" placeholder="Paste LSID cookie value here">
        </div>
        <div class="cookie-input">
            <label>__Secure-1PSIDTS</label>
            <input type="text" id="cookie-__Secure-1PSIDTS" placeholder="Paste __Secure-1PSIDTS cookie value here">
        </div>

        <p style="margin-top:16px">
            <button class="btn green" onclick="submitCookies()">Validate & Complete Setup</button>
            <button class="btn outline" onclick="skipCookies()">Skip (manual refresh)</button>
        </p>

        <div id="step2-status" class="status"></div>
    </div>

    <!-- STEP 3: Success -->
    <div class="card hidden" id="step3-card">
        <h2><span class="check">✓</span> Setup Complete!</h2>
        <p id="success-msg"></p>
    </div>
</div>

<script>
const CALLBACK_URL = '__CALLBACK_URL__';
let storedLoginHint = null;
let storedEmail = null;

// Check if we have token data in URL (from bookmarklet redirect)
(function() {
    var params = new URLSearchParams(window.location.search);
    var data = params.get('data');
    if (data) {
        try {
            var parsed = JSON.parse(data);
            handleTokenData(parsed);
        } catch(e) {
            showStatus('step1-status', 'Error parsing token data: ' + e.message, 'error');
        }
        // Clean URL
        history.replaceState({}, '', window.location.pathname);
    }
})();

function handleTokenData(data) {
    storedLoginHint = data.login_hint;
    storedEmail = data.email;

    // Send to server for validation
    showStatus('step1-status', 'Validating token...', 'info');

    fetch(CALLBACK_URL + '/token', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
    })
    .then(r => r.json())
    .then(result => {
        if (result.success) {
            showStatus('step1-status', 'Connected! Account: ' + (data.email || 'verified'), 'success');
            // Show step 2
            document.getElementById('step2-card').classList.remove('hidden');
            document.getElementById('step2-greeting').textContent =
                'Great! Connected as ' + data.email + '. Now let\\'s set up automatic refresh.';
        } else {
            showStatus('step1-status', 'Validation failed: ' + (result.error || 'unknown'), 'error');
        }
    })
    .catch(err => {
        showStatus('step1-status', 'Error: ' + err.message, 'error');
    });
}

function submitCookies() {
    var cookies = {
        'SID': document.getElementById('cookie-SID').value.trim(),
        '__Secure-3PSID': document.getElementById('cookie-__Secure-3PSID').value.trim(),
        'LSID': document.getElementById('cookie-LSID').value.trim(),
        '__Secure-1PSIDTS': document.getElementById('cookie-__Secure-1PSIDTS').value.trim(),
    };

    // Validate all cookies are provided
    for (var name in cookies) {
        if (!cookies[name]) {
            showStatus('step2-status', 'Please provide all 4 cookie values. Missing: ' + name, 'error');
            return;
        }
    }

    showStatus('step2-status', 'Validating cookies...', 'info');

    fetch(CALLBACK_URL + '/cookies', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cookies: cookies, login_hint: storedLoginHint})
    })
    .then(r => r.json())
    .then(result => {
        if (result.success) {
            showStatus('step2-status', 'Cookies validated! Refresh will work automatically.', 'success');
            showSuccess('Setup complete! Authenticated as ' + storedEmail +
                '. Token refresh is automated (cookies valid for 1+ year).');
        } else {
            showStatus('step2-status', 'Cookie validation failed: ' + (result.error || 'unknown') +
                '. Please check the cookie values are correct.', 'error');
        }
    })
    .catch(err => {
        showStatus('step2-status', 'Error: ' + err.message, 'error');
    });
}

function skipCookies() {
    showSuccess('Setup complete! Authenticated as ' + storedEmail +
        '. Note: Without cookies, you will need to re-authenticate when the token expires (1 hour).');
}

function showSuccess(msg) {
    document.getElementById('step3-card').classList.remove('hidden');
    document.getElementById('success-msg').textContent = msg;
}

function showStatus(elementId, msg, type) {
    var el = document.getElementById(elementId);
    el.className = 'status show ' + type;
    el.textContent = msg;
}
</script>
</body>
</html>"""


class GuidedSetupServer:
    """Server for the guided Nest setup flow."""

    def __init__(self):
        self.access_token: Optional[str] = None
        self.login_hint: Optional[str] = None
        self.email: Optional[str] = None
        self.cookies: Optional[dict] = None
        self.issue_token_url: Optional[str] = None
        self.done_event = asyncio.Event()

    async def start(self):
        app = web.Application()
        app.router.add_get("/", self.handle_setup_page)
        app.router.add_post("/callback/token", self.handle_token)
        app.router.add_post("/callback/cookies", self.handle_cookies)
        app.router.add_get("/status", self.handle_status)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, PROXY_HOST, PROXY_PORT)
        await site.start()
        return runner

    async def handle_setup_page(self, request: web.Request) -> web.Response:
        """Serve the setup page."""
        callback_url = f"{BASE_URL}/callback"

        # Build bookmarklet
        bookmarklet = BOOKMARKLET_CODE.replace("__CALLBACK_URL__", BASE_URL)
        bookmarklet_href = "javascript:" + urllib.parse.quote(bookmarklet)

        # Build console script (same as bookmarklet but readable)
        console_script = (
            f"var a=gapi.auth2.getAuthInstance().currentUser.get(),"
            f"r=a.getAuthResponse(true);"
            f"window.location='{BASE_URL}/?data='+encodeURIComponent("
            f"JSON.stringify({{access_token:r.access_token,login_hint:r.login_hint,"
            f"email:a.getBasicProfile().getEmail()}}))"
        )

        html = SETUP_PAGE_HTML
        html = html.replace("__CALLBACK_URL__", callback_url)
        html = html.replace("__BOOKMARKLET__", bookmarklet_href)
        html = html.replace("__CONSOLE_SCRIPT__", console_script)

        return web.Response(text=html, content_type="text/html")

    async def handle_token(self, request: web.Request) -> web.Response:
        """Receive and validate access_token + login_hint."""
        try:
            data = await request.json()
            access_token = data.get("access_token")
            login_hint = data.get("login_hint")
            email = data.get("email")

            if not access_token:
                return web.json_response({"success": False, "error": "no access_token"})

            print(f"\n  Received token for: {email}")
            print(f"  login_hint: {login_hint[:40] if login_hint else 'none'}...")

            # Validate the full chain
            jwt = self._issue_jwt(access_token)
            if not jwt:
                return web.json_response({"success": False, "error": "Token invalid (issue_jwt failed)"})

            session = self._get_session(jwt)
            if not session:
                return web.json_response({"success": False, "error": "Session creation failed"})

            self.access_token = access_token
            self.login_hint = login_hint
            self.email = email or session.get("email")

            print(f"  Validated! User: {self.email}")
            return web.json_response({"success": True})

        except Exception as e:
            return web.json_response({"success": False, "error": str(e)})

    async def handle_cookies(self, request: web.Request) -> web.Response:
        """Receive and validate cookies for long-term refresh."""
        try:
            data = await request.json()
            cookies = data.get("cookies", {})
            login_hint = data.get("login_hint") or self.login_hint

            if not login_hint:
                return web.json_response({"success": False, "error": "No login_hint (complete step 1 first)"})

            required = ['SID', '__Secure-3PSID', 'LSID', '__Secure-1PSIDTS']
            missing = [r for r in required if not cookies.get(r)]
            if missing:
                return web.json_response({"success": False, "error": f"Missing cookies: {missing}"})

            print(f"\n  Received cookies, validating issueToken...")

            # Test issueToken with these cookies
            cookie_str = "; ".join(f"{n}={v}" for n, v in cookies.items())
            token = self._test_issue_token(cookie_str, login_hint)

            if not token:
                return web.json_response({"success": False, "error": "issueToken failed - check cookie values"})

            # Validate the token works
            jwt = self._issue_jwt(token)
            if not jwt:
                return web.json_response({"success": False, "error": "Token from cookies is invalid"})

            # Success! Store everything
            self.cookies = cookies
            self.issue_token_url = self._build_issue_token_url(login_hint)

            print(f"  Cookie-based refresh VALIDATED!")
            print(f"  issue_token_url: {self.issue_token_url[:80]}...")
            self.done_event.set()

            return web.json_response({"success": True})

        except Exception as e:
            return web.json_response({"success": False, "error": str(e)})

    async def handle_status(self, request: web.Request) -> web.Response:
        """Check setup status."""
        return web.json_response({
            "authenticated": self.access_token is not None,
            "has_refresh": self.cookies is not None,
            "email": self.email,
        })

    def _build_issue_token_url(self, login_hint: str) -> str:
        """Construct the full issueToken URL."""
        params = {
            "action": "issueToken",
            "response_type": "token id_token",
            "login_hint": login_hint,
            "client_id": WEB_CLIENT_ID,
            "origin": "https://home.nest.com",
            "scope": "openid profile email https://www.googleapis.com/auth/nest-account",
            "ss_domain": "https://home.nest.com",
        }
        return f"{ISSUE_TOKEN_BASE}?{urllib.parse.urlencode(params)}"

    def _test_issue_token(self, cookie_str: str, login_hint: str) -> Optional[str]:
        """Test issueToken with cookies + login_hint."""
        url = self._build_issue_token_url(login_hint)
        req = urllib.request.Request(url, headers={
            "Sec-Fetch-Mode": "cors",
            "X-Requested-With": "XmlHttpRequest",
            "Referer": "https://accounts.google.com/o/oauth2/iframe",
            "Cookie": cookie_str,
        })
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read().decode()
                if body.startswith(")]}'"):
                    body = body[4:].strip()
                data = json.loads(body)
                token = data.get("access_token")
                if token:
                    print(f"  issueToken succeeded! Token: {token[:40]}...")
                    return token
                return None
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            print(f"  issueToken failed: {e.code} - {error_body[:100]}")
            return None

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
                return json.loads(resp.read()).get("jwt")
        except urllib.error.HTTPError as e:
            print(f"  issue_jwt failed: {e.code}")
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
            print(f"  session failed: {e.code}")
            return None


async def main():
    print("=" * 60)
    print("  Nest Auth PoC - Guided Setup")
    print("=" * 60)
    print()
    print("  This demonstrates the complete setup flow for HA:")
    print("  Step 1: Extract token + login_hint (bookmarklet)")
    print("  Step 2: Provide cookies for long-term refresh")
    print("  Result: Automated refresh for 1+ year!")
    print()
    print("  What gets stored in HA config:")
    print("  - issue_token URL (includes login_hint)")
    print("  - Cookie string (4 cookies, valid 1+ year)")
    print("  These are the SAME fields the current integration uses,")
    print("  but setup is guided instead of requiring DevTools knowledge.")

    server = GuidedSetupServer()
    runner = await server.start()

    print(f"\n  {'─' * 50}")
    print(f"  Open this URL in your browser:")
    print(f"  → {BASE_URL}/")
    print(f"  {'─' * 50}")
    print(f"\n  Waiting for setup completion (5 min timeout)...\n")

    try:
        await asyncio.wait_for(server.done_event.wait(), timeout=300)
    except asyncio.TimeoutError:
        print("\n  Timeout - setup not completed.")
        await runner.cleanup()
        return

    print(f"\n{'=' * 60}")
    print(f"  SETUP COMPLETE!")
    print(f"{'=' * 60}")
    print(f"  Email: {server.email}")
    print(f"  login_hint: {server.login_hint[:50]}...")
    print(f"  issue_token_url: {server.issue_token_url[:80]}...")
    print(f"  Cookies: {list(server.cookies.keys())}")
    print(f"\n  These values map directly to ha-nest-protect config:")
    print(f"  - issue_token: {server.issue_token_url[:60]}...")
    print(f"  - cookies: SID=...; __Secure-3PSID=...; LSID=...; __Secure-1PSIDTS=...")
    print(f"\n  The NestClient.get_access_token_from_cookies() method uses these")
    print(f"  to silently refresh tokens for 1+ year!")

    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
