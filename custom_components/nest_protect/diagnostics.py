"""Provides diagnostics for Nest Protect."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import HomeAssistantNestProtectData
from .const import DOMAIN
from .pynest.const import FULL_NEST_REQUEST
from .pynest.exceptions import NotAuthenticatedException
from .pynest.models import FirstDataAPIResponse

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


async def _async_get_first_data(
    entry_data: HomeAssistantNestProtectData,
    *,
    request: dict[str, Any] | None = None,
) -> FirstDataAPIResponse:
    """Fetch diagnostics data, retrying the exact session rejected by Nest."""
    client = entry_data.client
    manager = entry_data.session_manager
    await manager.ensure_session()
    session = client.nest_session
    if session is None:
        raise NotAuthenticatedException("No active Nest session")

    try:
        if request is None:
            return await client.get_first_data(session.access_token, session.userid)
        return await client.get_first_data(
            session.access_token, session.userid, request=request
        )
    except NotAuthenticatedException:
        await manager.async_refresh_session(rejected_session=session)

    replacement = client.nest_session
    if replacement is None:
        raise NotAuthenticatedException("Nest session recovery failed")
    try:
        if request is None:
            return await client.get_first_data(
                replacement.access_token, replacement.userid
            )
        return await client.get_first_data(
            replacement.access_token, replacement.userid, request=request
        )
    except NotAuthenticatedException:
        await manager.async_invalidate_session(rejected_session=replacement)
        raise


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]

    data = {
        "app_launch": dataclasses.asdict(
            await _async_get_first_data(entry_data, request=FULL_NEST_REQUEST)
        )
    }

    return async_redact_data(data, TO_REDACT)


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a device entry."""
    entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]

    data = {
        "device": {
            "controllable_name": device.hw_version,
            "firmware": device.sw_version,
            "model": device.model,
        },
        "app_launch": dataclasses.asdict(await _async_get_first_data(entry_data)),
    }

    return async_redact_data(data, TO_REDACT)
