"""Provides diagnostics for Nest Protect."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import HomeAssistantNestProtectData
from .const import CONF_COOKIES, CONF_ISSUE_TOKEN, CONF_REFRESH_TOKEN, DOMAIN
from .pynest.const import FULL_NEST_REQUEST

TO_REDACT = [
    "access_token",
    "address_lines",
    "aux_primary_fabric_id",
    "city",
    "country",
    "email",
    "emergency_contact_description",
    "emergency_contact_phone",
    "ifj_primary_fabric_id",
    "latitude",
    "location",
    "longitude",
    "name",
    "parameters",
    "pairing_token",
    "postal_code",
    "profile_image_url",
    "serial_number",
    "service_config",
    "state",
    "sunrise",
    "sunset",
    "temp_c",
    "thread_ip_address",
    "thread_mac_address",
    "time_zone",
    "topaz_hush_key",
    "user",
    "wifi_mac_address",
    "zip",
]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    issue_token = None
    cookies = None
    refresh_token = None

    if CONF_ISSUE_TOKEN in entry.data and CONF_COOKIES in entry.data:
        issue_token = entry.data[CONF_ISSUE_TOKEN]
        cookies = entry.data[CONF_COOKIES]
    if CONF_REFRESH_TOKEN in entry.data:
        refresh_token = entry.data[CONF_REFRESH_TOKEN]

    entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    client = entry_data.client

    if issue_token and cookies:
        auth = await client.get_access_token_from_cookies(issue_token, cookies)
    elif refresh_token:
        auth = await client.get_access_token_from_refresh_token(refresh_token)

    nest = await client.authenticate(auth.access_token)

    data = {
        "app_launch": dataclasses.asdict(
            await client.get_first_data(
                nest.access_token, nest.userid, request=FULL_NEST_REQUEST
            )
        )
    }

    return async_redact_data(data, TO_REDACT)


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a device entry."""
    issue_token = None
    cookies = None
    refresh_token = None

    if CONF_ISSUE_TOKEN in entry.data and CONF_COOKIES in entry.data:
        issue_token = entry.data[CONF_ISSUE_TOKEN]
        cookies = entry.data[CONF_COOKIES]
    if CONF_REFRESH_TOKEN in entry.data:
        refresh_token = entry.data[CONF_REFRESH_TOKEN]

    entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    client = entry_data.client

    if issue_token and cookies:
        auth = await client.get_access_token_from_cookies(issue_token, cookies)
    elif refresh_token:
        auth = await client.get_access_token_from_refresh_token(refresh_token)

    nest = await client.authenticate(auth.access_token)

    data = {
        "device": {
            "controllable_name": device.hw_version,
            "firmware": device.sw_version,
            "model": device.model,
        },
        "app_launch": dataclasses.asdict(
            await client.get_first_data(nest.access_token, nest.userid)
        ),
    }

    return async_redact_data(data, TO_REDACT)
