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
from .pynest.models import NestResponse

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


async def _async_current_session(
    entry_data: HomeAssistantNestProtectData,
) -> NestResponse:
    """Return the integration's own Nest session.

    Going through the session manager rather than signing in again keeps
    downloading diagnostics from rotating cookies that are then never persisted.
    """
    await entry_data.session_manager.ensure_session()
    return entry_data.client.nest_session


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    client = entry_data.client
    nest = await _async_current_session(entry_data)

    data = {
        "google_refresh": entry_data.session_manager.google_refresh_diagnostics,
        "app_launch": dataclasses.asdict(
            await client.get_first_data(
                nest.access_token, nest.userid, request=FULL_NEST_REQUEST
            )
        ),
    }

    return async_redact_data(data, TO_REDACT)


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a device entry."""
    entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    client = entry_data.client
    nest = await _async_current_session(entry_data)

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
