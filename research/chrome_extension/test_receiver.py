"""
Test receiver that simulates the HA-side callback endpoint.

Run this, then use the Chrome extension pointed at http://localhost:9876.
It will print whatever credentials the extension sends.

Usage:
    python test_receiver.py
"""

import asyncio

from aiohttp import web

HOST = "localhost"
PORT = 9876


async def handle_callback(request: web.Request) -> web.Response:
    data = await request.json()
    print("\n" + "=" * 60)  # noqa: T201
    print("  RECEIVED AUTH CREDENTIALS FROM EXTENSION")  # noqa: T201
    print("=" * 60)  # noqa: T201
    print(f"  Email:        {data.get('email', 'N/A')}")  # noqa: T201
    print(f"  issue_token:  {data.get('issue_token', 'N/A')[:80]}...")  # noqa: T201
    print(f"  cookies:      {len(data.get('cookies', ''))} chars")  # noqa: T201
    print(f"  access_token: {data.get('access_token', 'N/A')[:40]}...")  # noqa: T201

    cookie_names = [
        c.split("=")[0] for c in data.get("cookies", "").split("; ") if "=" in c
    ]
    print(f"  cookie names: {cookie_names}")  # noqa: T201
    print("=" * 60)  # noqa: T201
    print("\n  These would be stored in HA config entry as:")  # noqa: T201
    print("    issue_token: <url>")  # noqa: T201
    print("    cookies: <string>")  # noqa: T201
    print("  Ready for NestClient.get_access_token_from_cookies()!\n")  # noqa: T201

    return web.json_response({"status": "ok"})


async def handle_cors_preflight(request: web.Request) -> web.Response:
    return web.Response(
        status=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
    )


@web.middleware
async def cors_middleware(request, handler):
    resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


async def main():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_options("/api/nest_protect/auth_callback", handle_cors_preflight)
    app.router.add_post("/api/nest_protect/auth_callback", handle_callback)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()

    print(f"  Test receiver running at http://{HOST}:{PORT}")  # noqa: T201
    print(  # noqa: T201
        f"  Extension should POST to: http://{HOST}:{PORT}/api/nest_protect/auth_callback"
    )
    print("\n  Waiting for credentials from Chrome extension...\n")  # noqa: T201

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
