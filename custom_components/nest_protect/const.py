"""Constants for Nest Protect."""

from __future__ import annotations

import logging
from typing import Final

from homeassistant.const import Platform

from .pynest.const import SUBSCRIBE_TIMEOUT_SECONDS

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
SESSION_EXPIRY_BUFFER_SECONDS: Final = 300  # 5 minutes
# Minimum and maximum pause for all Google refreshes after a throttling response.
GOOGLE_THROTTLE_SECONDS: Final = 900  # 15 minutes
GOOGLE_THROTTLE_MAX_SECONDS: Final = 21600  # 6 hours
MAX_AUTH_FAILURES: Final = 3
BACKOFF_INTERVALS: Final = (30, 60, 120, 300, 600)  # seconds, capped at 10 min
# Floors for the subscriber's error backoff, which escalates from there.
SERVICE_ERROR_MIN_SECONDS: Final = 120
PYNEST_ERROR_MIN_SECONDS: Final = 60
# A lock stream rejected sooner than this after its last auth failure counts
# as the same failure streak, even if it delivered data in between.
LOCK_AUTH_SETTLE_SECONDS: Final = 300
RECONNECT_INTERVALS: Final = (5, 15, 30, 60, 120, 300)  # seconds, capped at 5 min
# A subscribe attempt that fails sooner than this counts as a connection failure;
# one that lasted longer ended a normal long-poll cycle.
SUBSCRIBE_HEALTHY_SECONDS: Final = 30
# Nest answering a subscribe with data sooner than this counts as a fast answer.
# One is normal (updates arrive in bursts). A run of them means we keep sending
# stale bucket revisions, the loop that once flooded the Nest API (#347/#374),
# so re-subscribes are spaced out and a warning is logged.
SUBSCRIBE_MIN_INTERVAL_SECONDS: Final = 2
SUBSCRIBE_FAST_RESPONSE_WARNING: Final = 10

# A healthy long-poll can stay silent for its full client timeout. A single
# failed request may itself run that long and pause before retrying, and then a
# quiet healthy poll follows, so allow two timeouts plus room for the longest
# retry pause before treating the stream as stale.
STREAM_RETRY_BUFFER_SECONDS: Final = 10 * 60
STREAM_STALE_AFTER_SECONDS: Final = (
    2 * SUBSCRIBE_TIMEOUT_SECONDS + STREAM_RETRY_BUFFER_SECONDS
)
