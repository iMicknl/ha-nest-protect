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

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
]

STORAGE_VERSION: Final = 1
STORAGE_KEY_FORMAT: Final = "nest_protect_{entry_id}"
SESSION_EXPIRY_BUFFER_SECONDS: Final = 300  # 5 minutes
MAX_AUTH_FAILURES: Final = 3
BACKOFF_INTERVALS: Final = (30, 60, 120, 300, 600)  # seconds, capped at 10 min
