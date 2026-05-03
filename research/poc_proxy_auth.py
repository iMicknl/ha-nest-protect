"""
Nest Authentication PoC - Proxy Method

This demonstrates the AuthCaptureProxy pattern used by Tesla Custom Integration
adapted for the Nest/Google OAuth flow. This is the approach that would work
for all Home Assistant users via a custom component config flow.

How it works:
1. Start a local reverse proxy that mimics home.nest.com
2. Rewrite the Google OAuth flow to redirect through OUR proxy instead of home.nest.com
3. When Google redirects back with the token, WE intercept it
4. Use the captured access_token → issue_jwt → /session chain

The key insight: Google's GIS SDK (gsiwebsdk=2) uses response_type=permission+id_token
which delivers the access_token via a postMessage from the consent page back to the
origin. But if we proxy the login page, we ARE the origin.

Alternative approach (simpler): Since the Nest login page loads gapi.auth2 and the
OAuth iframe does a silent token grant when Google cookies are present, we can:
1. Proxy accounts.google.com/o/oauth2/iframe requests
2. Capture the issueToken response containing the access_token

Simplest approach (implemented here): Proxy the entire Nest login flow and capture
the nestauthproxyservice issue_jwt request which contains the access_token in the body.

Usage:
    python poc_proxy_auth.py

Then open http://localhost:8123/auth/nest_protect/login in your browser.
After you sign in with Google, the proxy captures your access token.

Requirements:
    pip install aiohttp
"""

import asyncio
import json
import ssl
import urllib.parse
import urllib.request
import urllib.error
from aiohttp import web, ClientSession, TCPConnector


# === Constants ===
WEB_CLIENT_ID = "733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com"
SCOPES = "openid profile email https://www.googleapis.com/auth/nest-account"
ISSUE_JWT_URL = "https://nestauthproxyservice-pa.googleapis.com/v1/issue_jwt"
SESSION_URL = "https://home.nest.com/session"

PROXY_HOST = "localhost"
PROXY_PORT = 9876
PROXY_BASE = f"http://{PROXY_HOST}:{PROXY_PORT}"
AUTH_PATH = "/auth/nest_protect"


class NestAuthProxy:
    """
    Reverse proxy that intercepts the Nest/Google OAuth flow.

    Strategy:
    We don't actually need to proxy everything. The flow is:
    1. User opens our login URL
    2. We redirect them to Google's OAuth with OUR callback URL
    3. Google authenticates and redirects to home.nest.com/login/callback
       (which we can't change - it's baked into the web client_id)

    So instead, we take a different approach:
    - We proxy the home.nest.com login page through us
    - The page loads, initiates OAuth with Google
    - The OAuth iframe / GIS SDK gets a token silently (if Google session exists)
    - OR the user clicks "Sign in with Google" and goes through consent
    - Either way, the page eventually calls issue_jwt with the access_token
    - We intercept that XHR call and capture the token

    For HA integration, this would be:
    - Register a HomeAssistantView at /auth/nest_protect/proxy/**
    - Proxy all requests to home.nest.com (rewriting URLs)
    - Monitor for the issue_jwt POST body containing google_oauth_access_token
    """

    def __init__(self):
        self.captured_token = None
        self.token_event = asyncio.Event()
        self.session = None

    async def start(self):
        app = web.Application()
        app.router.add_get(f"{AUTH_PATH}/login", self.handle_login_redirect)
        app.router.add_route("*", f"{AUTH_PATH}/proxy/{{path:.*}}", self.handle_proxy)
        app.router.add_get(f"{AUTH_PATH}/done", self.handle_done)

        ssl_ctx = ssl.create_default_context()
        self.session = ClientSession(connector=TCPConnector(ssl=ssl_ctx))

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, PROXY_HOST, PROXY_PORT)
        await site.start()
        return runner

    async def handle_login_redirect(self, request: web.Request) -> web.Response:
        """Entry point: redirect user to our proxied version of home.nest.com"""
        proxy_url = f"{PROXY_BASE}{AUTH_PATH}/proxy/home.nest.com/"
        return web.HTTPFound(proxy_url)

    async def handle_proxy(self, request: web.Request) -> web.Response:
        """
        Reverse proxy handler - forwards requests to the real server
        and intercepts auth-related responses.
        """
        path = request.match_info["path"]

        # Parse the target from the path: proxy/home.nest.com/path/to/resource
        parts = path.split("/", 1)
        if len(parts) < 1:
            return web.Response(text="Invalid proxy path", status=400)

        target_host = parts[0]
        target_path = "/" + parts[1] if len(parts) > 1 else "/"
        target_url = f"https://{target_host}{target_path}"

        if request.query_string:
            target_url += f"?{request.query_string}"

        print(f"  [PROXY] {request.method} {target_host}{target_path[:60]}")

        # Read request body
        body = await request.read()

        # Check if this is the issue_jwt call - INTERCEPT!
        if "nestauthproxyservice" in target_host and "issue_jwt" in target_path:
            return await self._intercept_issue_jwt(request, target_url, body)

        # Forward the request
        headers = dict(request.headers)
        headers.pop("Host", None)
        headers.pop("host", None)
        headers["Host"] = target_host

        # Fix referer/origin to point to the real server
        if "Referer" in headers:
            headers["Referer"] = headers["Referer"].replace(
                f"{PROXY_BASE}{AUTH_PATH}/proxy/{target_host}", f"https://{target_host}"
            )
        if "Origin" in headers:
            headers["Origin"] = f"https://{target_host}"

        try:
            async with self.session.request(
                method=request.method,
                url=target_url,
                headers=headers,
                data=body if body else None,
                allow_redirects=False,
            ) as resp:
                resp_body = await resp.read()
                resp_headers = dict(resp.headers)

                # Handle redirects - rewrite Location to go through proxy
                if resp.status in (301, 302, 307, 308):
                    location = resp_headers.get("Location", "")
                    if location:
                        resp_headers["Location"] = self._rewrite_url(location, target_host)
                    return web.Response(
                        status=resp.status,
                        headers={k: v for k, v in resp_headers.items()
                                 if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")},
                    )

                # Rewrite HTML/JS content to route through proxy
                content_type = resp_headers.get("Content-Type", "")
                if any(ct in content_type for ct in ["text/html", "javascript", "application/json"]):
                    text = resp_body.decode("utf-8", errors="replace")
                    text = self._rewrite_content(text, target_host)
                    resp_body = text.encode("utf-8")

                # Remove problematic headers
                clean_headers = {
                    k: v for k, v in resp_headers.items()
                    if k.lower() not in (
                        "transfer-encoding", "content-encoding",
                        "content-length", "content-security-policy",
                        "x-frame-options", "strict-transport-security",
                    )
                }
                clean_headers["Content-Length"] = str(len(resp_body))
                # Allow cross-origin for the OAuth iframe
                clean_headers["Access-Control-Allow-Origin"] = "*"

                return web.Response(
                    status=resp.status,
                    body=resp_body,
                    headers=clean_headers,
                )
        except Exception as e:
            return web.Response(text=f"Proxy error: {e}", status=502)

    async def _intercept_issue_jwt(self, request: web.Request, target_url: str, body: bytes) -> web.Response:
        """Intercept the issue_jwt call to capture the Google access token."""
        try:
            payload = json.loads(body)
            access_token = payload.get("google_oauth_access_token")
            if access_token:
                print(f"\n{'*' * 60}")
                print(f"  CAPTURED ACCESS TOKEN!")
                print(f"  Token: {access_token[:50]}...")
                print(f"{'*' * 60}\n")
                self.captured_token = access_token
                self.token_event.set()
        except (json.JSONDecodeError, KeyError):
            pass

        # Still forward the request so the page works normally
        headers = dict(request.headers)
        headers.pop("Host", None)
        headers.pop("host", None)

        async with self.session.request(
            method="POST",
            url=target_url,
            headers=headers,
            data=body,
            allow_redirects=False,
        ) as resp:
            resp_body = await resp.read()
            resp_headers = {
                k: v for k, v in resp.headers.items()
                if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
            }
            resp_headers["Content-Length"] = str(len(resp_body))
            resp_headers["Access-Control-Allow-Origin"] = "*"
            resp_headers["Access-Control-Allow-Headers"] = "*"
            return web.Response(status=resp.status, body=resp_body, headers=resp_headers)

    async def handle_done(self, request: web.Request) -> web.Response:
        """Success page shown after token capture."""
        return web.Response(
            text="<html><body><h2>Authentication successful!</h2>"
                 "<p>You can close this window and return to Home Assistant.</p>"
                 "</body></html>",
            content_type="text/html",
        )

    def _rewrite_url(self, url: str, current_host: str) -> str:
        """Rewrite an absolute URL to go through our proxy."""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme in ("https", "http") and parsed.netloc:
            result = f"{PROXY_BASE}{AUTH_PATH}/proxy/{parsed.netloc}{parsed.path}"
            if parsed.query:
                result += f"?{parsed.query}"
            return result
        if url.startswith("/"):
            return f"{PROXY_BASE}{AUTH_PATH}/proxy/{current_host}{url}"
        return url

    def _rewrite_content(self, content: str, target_host: str) -> str:
        """Rewrite URLs in HTML/JS content to route through proxy."""
        # Inject <base> tag so relative URLs resolve through proxy
        base_tag = f'<base href="{PROXY_BASE}{AUTH_PATH}/proxy/{target_host}/">'
        content = content.replace("<head>", f"<head>{base_tag}", 1)

        # Rewrite absolute URLs for known hosts
        hosts_to_proxy = [
            "home.nest.com",
            "nestauthproxyservice-pa.googleapis.com",
        ]
        for host in hosts_to_proxy:
            content = content.replace(
                f"https://{host}",
                f"{PROXY_BASE}{AUTH_PATH}/proxy/{host}"
            )
        return content


async def validate_token(access_token: str) -> bool:
    """Validate the full chain: access_token -> JWT -> session."""
    print("\nValidating token chain...")
    print(f"  Access token: {access_token[:40]}...")

    # issue_jwt
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
            jwt = json.loads(resp.read())["jwt"]
            print(f"  JWT obtained: {jwt[:50]}...")
    except urllib.error.HTTPError as e:
        print(f"  FAILED at issue_jwt: {e.code} - {e.read().decode()[:200]}")
        return False

    # session
    req = urllib.request.Request(SESSION_URL, headers={
        "Authorization": f"Basic {jwt}",
        "X-Requested-With": "XMLHttpRequest",
    })

    try:
        with urllib.request.urlopen(req) as resp:
            session = json.loads(resp.read())
            print(f"  Session created!")
            print(f"    User: {session.get('email')}")
            print(f"    UserID: {session.get('userid')}")
            print(f"    Transport: {session.get('urls', {}).get('transport_url', '')}")
            return True
    except urllib.error.HTTPError as e:
        print(f"  FAILED at session: {e.code} - {e.read().decode()[:200]}")
        return False


async def main():
    print("=" * 60)
    print("  Nest Auth PoC - Reverse Proxy Method")
    print("=" * 60)
    print(f"\n  This simulates the AuthCaptureProxy pattern used by")
    print(f"  Tesla Custom Integration, adapted for Nest/Google OAuth.")
    print(f"\n  The proxy intercepts the issue_jwt call to capture")
    print(f"  the Google access_token without needing cookies or")
    print(f"  a registered redirect_uri.")

    proxy = NestAuthProxy()
    runner = await proxy.start()

    login_url = f"{PROXY_BASE}{AUTH_PATH}/login"
    print(f"\n  Proxy started on {PROXY_BASE}")
    print(f"\n  {'─' * 50}")
    print(f"  Open this URL in your browser:")
    print(f"  → {login_url}")
    print(f"  {'─' * 50}")
    print(f"\n  Sign in with your Google account when prompted.")
    print(f"  The proxy will capture the access token automatically.\n")

    # Wait for token capture
    try:
        await asyncio.wait_for(proxy.token_event.wait(), timeout=300)
    except asyncio.TimeoutError:
        print("\n  Timeout after 5 minutes. No token captured.")
        await proxy.session.close()
        await runner.cleanup()
        return

    # Validate the captured token
    if proxy.captured_token:
        success = await validate_token(proxy.captured_token)
        if success:
            print(f"\n{'=' * 60}")
            print(f"  SUCCESS - Full auth chain validated!")
            print(f"  This token can now be used by the HA integration.")
            print(f"{'=' * 60}")
        else:
            print(f"\n  Token captured but validation failed.")

    await proxy.session.close()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
