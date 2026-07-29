"""Constants for Nest Protect."""

from __future__ import annotations

import logging
from typing import Final

from homeassistant.const import Platform

LOGGER: logging.Logger = logging.getLogger(__package__)

DOMAIN: Final = "nest_protect"
ATTRIBUTION: Final = "Data provided by Google"

CONF_ACCOUNT_TYPE: Final = "account_type"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_ISSUE_TOKEN: Final = "issue_token"
CONF_COOKIES: Final = "cookies"
CONF_AUTH_CODE: Final = "auth_code"
CONF_AUTH_GENERATION: Final = "auth_generation"
CONF_PREVIOUS_AUTH_GENERATION: Final = "previous_auth_generation"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.LOCK,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
]

STORAGE_VERSION: Final = 1
STORAGE_KEY_FORMAT: Final = "nest_protect_{entry_id}"
STORAGE_PENDING_REAUTH_KEY_FORMAT: Final = "nest_protect_pending_reauth_{entry_id}"
SESSION_EXPIRY_BUFFER_SECONDS: Final = 300  # 5 minutes
AUTH_RETRY_DELAYS: Final = (1, 5, 30)
