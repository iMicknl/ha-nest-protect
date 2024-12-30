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

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
]
