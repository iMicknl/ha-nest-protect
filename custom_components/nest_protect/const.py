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
    Platform.LOCK,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
]

STORAGE_VERSION: Final = 1
STORAGE_KEY_FORMAT: Final = "nest_protect_{entry_id}"
# Separate store: the session store is rewritten wholesale on every refresh.
STORAGE_KEY_DEVICES_FORMAT: Final = "nest_protect_devices_{entry_id}"
# Coalesce the burst of traits that arrives when the observe stream connects.
DEVICE_CACHE_SAVE_DELAY: Final = 10
SESSION_EXPIRY_BUFFER_SECONDS: Final = 300  # 5 minutes
# Reconnect pacing for the protobuf observe stream. The gateway hangs up on its
# own schedule, so a fixed short delay would mean reconnecting every few
# seconds for weeks on an account it doesn't want to keep a stream open for.
PROTOBUF_RECONNECT_INITIAL_DELAY: Final = 5
PROTOBUF_RECONNECT_MAX_DELAY: Final = 300
# A stream that stayed up this long is treated as healthy, so the next
# reconnect starts from the floor again instead of inheriting the backoff.
PROTOBUF_STREAM_HEALTHY_SECONDS: Final = 60
MAX_AUTH_FAILURES: Final = 3
BACKOFF_INTERVALS: Final = (30, 60, 120, 300, 600)  # seconds, capped at 10 min
