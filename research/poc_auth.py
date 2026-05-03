"""
Nest Authentication Proof of Concept

Tests multiple auth approaches to find the most reliable method
for obtaining and refreshing Google access tokens with nest-account scope.

Usage:
    python poc_auth.py [--method METHOD]

Methods:
    1. loopback   - Local HTTP server receives OAuth redirect (implicit flow)
    2. intercept  - Opens Nest OAuth flow in browser, intercepts the access token
    3. refresh    - Tests an existing refresh token (if you have one)
    4. cookies    - Tests the legacy cookie/issueToken approach

Requirements:
    pip install aiohttp
"""

import argparse
import asyncio
import json
import hashlib
import os
import secrets
import sys
import urllib.parse
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import webbrowser
import time


# === Constants ===

# Web client (used by home.nest.com - confidential, needs client_secret for code exchange)
WEB_CLIENT_ID = "733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com"

# iOS client (legacy - OOB deprecated, cannot obtain new tokens)
IOS_CLIENT_ID = "733249279899-1gpkq9duqmdp55a7e5lft1pr2smumdla.apps.googleusercontent.com"

NEST_API_KEY = "AIzaSyAdkSIMNc51XGNEAYWasX9UOWkS5P6sZE4"
SCOPES = "openid profile email https://www.googleapis.com/auth/nest-account"

ISSUE_JWT_URL = "https://nestauthproxyservice-pa.googleapis.com/v1/issue_jwt"
SESSION_URL = "https://home.nest.com/session"
TOKEN_URL = "https://oauth2.googleapis.com/token"


# === Shared Utilities ===

def issue_jwt(access_token: str) -> str:
    """Exchange a Google access token for a Nest JWT."""
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

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["jwt"]


def get_session(jwt: str) -> dict:
    """Create a Nest session using a JWT."""
    req = urllib.request.Request(SESSION_URL, headers={
        "Authorization": f"Basic {jwt}",
        "X-Requested-With": "XMLHttpRequest",
    })

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def validate_token_chain(access_token: str) -> bool:
    """Validate the full chain: access_token -> JWT -> session."""
    print(f"\n{'─'*50}")
    print("Validating token chain...")
    print(f"  Access token: {access_token[:40]}...")

    try:
        jwt = issue_jwt(access_token)
        print(f"  JWT obtained: {jwt[:50]}...")
    except urllib.error.HTTPError as e:
        print(f"  FAILED at issue_jwt: {e.code} - {e.read().decode()[:200]}")
        return False

    try:
        session = get_session(jwt)
        print(f"  Session created!")
        print(f"    User: {session.get('email')}")
        print(f"    UserID: {session.get('userid')}")
        print(f"    Transport: {session.get('urls', {}).get('transport_url', '')}")
        print(f"    Expires in: {session.get('expires_in')}s")
        return True
    except urllib.error.HTTPError as e:
        print(f"  FAILED at session: {e.code} - {e.read().decode()[:200]}")
        return False


# === Method 1: Loopback OAuth (Implicit Flow) ===

class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler that captures the OAuth token from the redirect."""

    access_token = None
    auth_code = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/callback":
            # The token comes in the URL fragment (#), which browsers don't send to servers.
            # So we serve a page that extracts it from the fragment and posts it back.
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html><html><body>
<h2>Extracting token...</h2>
<script>
    // Token is in the URL fragment (after #)
    const fragment = window.location.hash.substring(1);
    const params = new URLSearchParams(fragment);
    const token = params.get('access_token');
    if (token) {
        fetch('/token?access_token=' + encodeURIComponent(token))
            .then(() => { document.body.innerHTML = '<h2>Success! You can close this tab.</h2>'; });
    } else {
        // Maybe it's a code flow response
        const code = new URLSearchParams(window.location.search).get('code');
        if (code) {
            fetch('/token?code=' + encodeURIComponent(code))
                .then(() => { document.body.innerHTML = '<h2>Success! You can close this tab.</h2>'; });
        } else {
            document.body.innerHTML = '<h2>No token found. Fragment: ' + fragment + '</h2>';
        }
    }
</script></body></html>""")

        elif parsed.path == "/token":
            # Receive the token from the client-side script
            if "access_token" in params:
                OAuthCallbackHandler.access_token = params["access_token"][0]
                print(f"\n  [Server] Received access token!")
            elif "code" in params:
                OAuthCallbackHandler.auth_code = params["code"][0]
                print(f"\n  [Server] Received auth code!")

            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress logs


def method_loopback():
    """
    Method 1: Start a local server and use OAuth implicit flow.

    NOTE: This will likely fail because the web client_id only has
    https://home.nest.com/login/callback as a registered redirect_uri.
    This PoC demonstrates what WOULD work if we had a registered loopback URI.
    """
    print("\n" + "=" * 60)
    print("METHOD 1: Loopback OAuth (Implicit Flow)")
    print("=" * 60)

    port = 8888
    server = HTTPServer(("localhost", port), OAuthCallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Build OAuth URL - try implicit flow (response_type=token)
    # This uses the web client_id with a loopback redirect
    auth_url = (
        "https://accounts.google.com/o/oauth2/auth?"
        + urllib.parse.urlencode({
            "client_id": WEB_CLIENT_ID,
            "redirect_uri": f"http://localhost:{port}/callback",
            "response_type": "token",
            "scope": SCOPES,
            "prompt": "consent",
        })
    )

    print(f"\n  Redirect URI: http://localhost:{port}/callback")
    print(f"  Opening browser...")
    print(f"  URL: {auth_url[:100]}...")
    print(f"\n  NOTE: This will likely fail with 'redirect_uri_mismatch'")
    print(f"  because Google only allows registered redirect URIs.")

    webbrowser.open(auth_url)

    # Wait for token
    print("\n  Waiting for OAuth response (30s timeout)...")
    for _ in range(30):
        if OAuthCallbackHandler.access_token:
            validate_token_chain(OAuthCallbackHandler.access_token)
            server.shutdown()
            return True
        time.sleep(1)

    print("\n  Timeout - no token received.")
    print("  Expected: redirect_uri_mismatch error from Google.")
    server.shutdown()
    return False


# === Method 2: Intercept Nest OAuth Flow ===

def method_intercept():
    """
    Method 2: Navigate the user through the real Nest OAuth flow and intercept
    the access token from the page after login completes.

    This opens the actual home.nest.com login, which uses the GIS SDK to obtain
    an access token after the user authenticates. We then extract that token
    by connecting to the browser via CDP (Chrome DevTools Protocol).

    Requires: Browser running with --remote-debugging-port=9222
    """
    print("\n" + "=" * 60)
    print("METHOD 2: Intercept Nest OAuth Flow (CDP)")
    print("=" * 60)

    print("\n  This method connects to a browser with remote debugging enabled.")
    print("  It opens home.nest.com, waits for login, and extracts the token.")
    print("\n  Prerequisites:")
    print("    - Browser running with: --remote-debugging-port=9222")
    print("    - Google account logged in")

    try:
        # Check if browser is available
        resp = urllib.request.urlopen("http://127.0.0.1:9222/json/version")
        version = json.loads(resp.read())
        print(f"\n  Connected to: {version.get('Browser', 'unknown')}")
    except Exception:
        print("\n  ERROR: Cannot connect to browser on port 9222.")
        print("  Start your browser with: --remote-debugging-port=9222")
        return False

    try:
        import websockets
    except ImportError:
        print("\n  ERROR: 'websockets' package required. Install with: pip install websockets")
        return False

    async def intercept_flow():
        # Open a new tab
        req = urllib.request.Request(
            "http://127.0.0.1:9222/json/new?about:blank",
            method="PUT"
        )
        resp = urllib.request.urlopen(req)
        tab = json.loads(resp.read())
        ws_url = tab["webSocketDebuggerUrl"]
        tab_id = tab["id"]
        print(f"  Opened tab: {tab_id[:12]}")

        async with websockets.connect(ws_url, max_size=50 * 1024 * 1024) as ws:
            # Enable network + page events
            await ws.send(json.dumps({"id": 1, "method": "Network.enable", "params": {}}))
            await ws.recv()
            await ws.send(json.dumps({"id": 2, "method": "Page.enable", "params": {}}))
            await ws.recv()

            # Navigate to home.nest.com
            await ws.send(json.dumps({"id": 3, "method": "Page.navigate", "params": {
                "url": "https://home.nest.com/"
            }}))
            await ws.recv()

            print("\n  Navigated to home.nest.com")
            print("  Waiting for authentication (120s timeout)...")
            print("  → If you see the login page, click 'Sign in with Google'")

            # Monitor for the issue_jwt call which contains the access token
            access_token = None
            start = time.time()
            timeout = 120

            try:
                while time.time() - start < timeout:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    data = json.loads(msg)

                    if data.get("method") == "Network.requestWillBeSent":
                        req_data = data["params"]["request"]
                        url = req_data["url"]

                        if "nestauthproxyservice" in url and "issue_jwt" in url:
                            post_data = req_data.get("postData", "")
                            if "google_oauth_access_token" in post_data:
                                body = json.loads(post_data)
                                access_token = body["google_oauth_access_token"]
                                print(f"\n  Intercepted access token from issue_jwt call!")
                                break

            except asyncio.TimeoutError:
                pass

            # Close the tab
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:9222/json/close/{tab_id}",
                    method="PUT"
                )
                urllib.request.urlopen(req)
            except Exception:
                pass

            if access_token:
                return validate_token_chain(access_token)
            else:
                print("\n  Timeout - no access token intercepted.")
                print("  Make sure to complete the Google sign-in flow in the browser.")
                return False

    return asyncio.run(intercept_flow())


# === Method 3: Refresh Token ===

def method_refresh(refresh_token: str = None):
    """
    Method 3: Use an existing refresh token to get a new access token.

    The iOS client_id can no longer ISSUE new refresh tokens (OOB deprecated),
    but existing refresh tokens may still work for token exchange.
    """
    print("\n" + "=" * 60)
    print("METHOD 3: Refresh Token Exchange")
    print("=" * 60)

    if not refresh_token:
        refresh_token = os.environ.get("NEST_REFRESH_TOKEN", "")

    if not refresh_token:
        print("\n  No refresh token provided.")
        print("  Set NEST_REFRESH_TOKEN env var or pass --refresh-token")
        print("\n  NOTE: New refresh tokens CANNOT be obtained since Jan 2023")
        print("  (Google deprecated OOB redirect for the iOS client_id)")
        return False

    print(f"\n  Testing refresh token: {refresh_token[:20]}...")
    print(f"  Client ID: {IOS_CLIENT_ID[:40]}...")

    payload = urllib.parse.urlencode({
        "refresh_token": refresh_token,
        "client_id": IOS_CLIENT_ID,
        "grant_type": "refresh_token",
    }).encode()

    req = urllib.request.Request(TOKEN_URL, data=payload, headers={
        "Content-Type": "application/x-www-form-urlencoded",
    })

    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read())
            access_token = body["access_token"]
            print(f"\n  Refresh succeeded!")
            print(f"  Access token: {access_token[:40]}...")
            print(f"  Expires in: {body.get('expires_in')}s")
            print(f"  Scope: {body.get('scope')}")
            return validate_token_chain(access_token)
    except urllib.error.HTTPError as e:
        error = e.read().decode()
        print(f"\n  Refresh FAILED: {e.code}")
        print(f"  Error: {error[:300]}")
        return False


# === Method 4: Cookie/IssueToken (Legacy) ===

def method_cookies(issue_token_url: str = None, cookies: str = None):
    """
    Method 4: Use Google session cookies + issueToken URL (current ha-nest-protect method).

    This is what the integration currently does - it's unreliable because
    Google session cookies expire unpredictably.
    """
    print("\n" + "=" * 60)
    print("METHOD 4: Cookie + IssueToken (Legacy)")
    print("=" * 60)

    if not issue_token_url:
        issue_token_url = os.environ.get("NEST_ISSUE_TOKEN", "")
    if not cookies:
        cookies = os.environ.get("NEST_COOKIES", "")

    if not issue_token_url or not cookies:
        print("\n  No issue_token URL or cookies provided.")
        print("  Set NEST_ISSUE_TOKEN and NEST_COOKIES env vars")
        print("\n  To get these values:")
        print("  1. Open home.nest.com in browser (logged in)")
        print("  2. Open DevTools → Network tab")
        print("  3. Find request to accounts.google.com/o/oauth2/iframerpc")
        print("  4. Copy the full Request URL as issue_token")
        print("  5. Copy the Cookie header value as cookies")
        return False

    print(f"\n  Issue token URL: {issue_token_url[:80]}...")
    print(f"  Cookies: [present, length={len(cookies)}]")

    req = urllib.request.Request(issue_token_url, headers={
        "Sec-Fetch-Mode": "cors",
        "X-Requested-With": "XmlHttpRequest",
        "Referer": "https://accounts.google.com/o/oauth2/iframe",
        "cookie": cookies,
    })

    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read())
            access_token = body.get("access_token")
            if access_token:
                print(f"\n  issueToken succeeded!")
                print(f"  Access token: {access_token[:40]}...")
                print(f"  Expires in: {body.get('expires_in')}s")
                return validate_token_chain(access_token)
            else:
                print(f"\n  No access_token in response: {json.dumps(body)[:200]}")
                return False
    except urllib.error.HTTPError as e:
        error = e.read().decode()
        print(f"\n  issueToken FAILED: {e.code}")
        print(f"  Error: {error[:300]}")
        return False


# === Method 5: Direct Access Token Test ===

def method_direct(access_token: str = None):
    """
    Method 5: Test a directly provided access token.

    Useful for validating that the issue_jwt → session chain works.
    """
    print("\n" + "=" * 60)
    print("METHOD 5: Direct Access Token Test")
    print("=" * 60)

    if not access_token:
        access_token = os.environ.get("NEST_ACCESS_TOKEN", "")

    if not access_token:
        print("\n  No access token provided.")
        print("  Set NEST_ACCESS_TOKEN env var or pass --access-token")
        return False

    return validate_token_chain(access_token)


# === Main ===

def main():
    parser = argparse.ArgumentParser(description="Nest Auth PoC - Test authentication methods")
    parser.add_argument("--method", choices=["loopback", "intercept", "refresh", "cookies", "direct", "all"],
                        default="all", help="Auth method to test")
    parser.add_argument("--refresh-token", help="Google refresh token to test")
    parser.add_argument("--access-token", help="Google access token to test directly")
    parser.add_argument("--issue-token", help="Google issueToken URL")
    parser.add_argument("--cookies", help="Google session cookies")
    args = parser.parse_args()

    print("=" * 60)
    print("  Nest Authentication Proof of Concept")
    print("=" * 60)
    print(f"\n  Web Client ID: {WEB_CLIENT_ID}")
    print(f"  iOS Client ID: {IOS_CLIENT_ID}")
    print(f"  Scopes: {SCOPES}")

    results = {}

    if args.method in ("direct", "all"):
        if args.access_token or os.environ.get("NEST_ACCESS_TOKEN"):
            results["direct"] = method_direct(args.access_token)

    if args.method in ("refresh", "all"):
        if args.refresh_token or os.environ.get("NEST_REFRESH_TOKEN"):
            results["refresh"] = method_refresh(args.refresh_token)

    if args.method in ("cookies", "all"):
        if (args.issue_token or os.environ.get("NEST_ISSUE_TOKEN")) and \
           (args.cookies or os.environ.get("NEST_COOKIES")):
            results["cookies"] = method_cookies(args.issue_token, args.cookies)

    if args.method in ("intercept",):
        results["intercept"] = method_intercept()

    if args.method in ("loopback",):
        results["loopback"] = method_loopback()

    if args.method == "all" and not results:
        print("\n\n  No credentials provided. Run with specific methods:")
        print("    python poc_auth.py --method intercept")
        print("    python poc_auth.py --method refresh --refresh-token <token>")
        print("    python poc_auth.py --method direct --access-token <ya29...>")
        print("    python poc_auth.py --method loopback")
        print("\n  Or set environment variables:")
        print("    NEST_REFRESH_TOKEN, NEST_ACCESS_TOKEN, NEST_ISSUE_TOKEN, NEST_COOKIES")
        return

    # Summary
    print("\n\n" + "=" * 60)
    print("  RESULTS SUMMARY")
    print("=" * 60)
    for method, success in results.items():
        status = "PASS" if success else "FAIL"
        print(f"  {method:12s}: {status}")


if __name__ == "__main__":
    main()
