"""HTTP view to receive auth credentials from the Chrome extension."""

from __future__ import annotations

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import CONF_COOKIES, CONF_ISSUE_TOKEN, DOMAIN, LOGGER


class NestProtectAuthCallbackView(HomeAssistantView):
    """Handle auth callback from the Chrome extension."""

    url = "/api/nest_protect/auth_callback"
    name = "api:nest_protect:auth_callback"
    requires_auth = False
    cors_allowed = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize."""
        self.hass = hass
        self._waiting_flow_id: str | None = None

    def register_flow(self, flow_id: str) -> None:
        """Register a config flow that is waiting for credentials."""
        self._waiting_flow_id = flow_id

    def unregister_flow(self, flow_id: str) -> None:
        """Unregister a waiting flow."""
        if self._waiting_flow_id == flow_id:
            self._waiting_flow_id = None

    async def post(self, request: web.Request) -> web.Response:
        """Handle POST from Chrome extension."""
        try:
            data = await request.json()
        except ValueError:
            return self._error("invalid_json", status=400)

        issue_token = data.get("issue_token", "").strip()
        cookies = data.get("cookies", "").strip()

        if not issue_token or not cookies:
            return self._error(
                "missing_credentials",
                "issue_token and cookies are required",
                status=400,
            )

        if "action=issueToken" not in issue_token:
            return self._error("invalid_issue_token", status=400)

        LOGGER.debug(
            "Auth callback received - issue_token: %s..., cookies length: %d, cookie names: %s",
            issue_token[:80],
            len(cookies),
            [c.split("=")[0] for c in cookies.split("; ")],
        )

        if not self._waiting_flow_id:
            return self._error(
                "no_active_flow",
                "Start setup in Home Assistant first, then use the extension.",
                status=409,
            )

        flow_id = self._waiting_flow_id

        try:
            await self.hass.config_entries.flow.async_configure(
                flow_id=flow_id,
                user_input={
                    CONF_ISSUE_TOKEN: issue_token,
                    CONF_COOKIES: cookies,
                },
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception("Failed to configure flow %s", flow_id)
            return self._error("flow_error", status=500)

        return web.json_response({"status": "ok"})

    @staticmethod
    def _error(code: str, detail: str = "", status: int = 400) -> web.Response:
        """Return a JSON error response."""
        body = {"error": code}
        if detail:
            body["detail"] = detail
        return web.json_response(body, status=status)


def async_get_auth_view(hass: HomeAssistant) -> NestProtectAuthCallbackView:
    """Get or register the auth callback view singleton."""
    key = f"{DOMAIN}_auth_view"
    if key not in hass.data:
        view = NestProtectAuthCallbackView(hass)
        hass.http.register_view(view)
        hass.data[key] = view
    return hass.data[key]
