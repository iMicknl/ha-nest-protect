"""Provides diagnostics for Nest Protect."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import HomeAssistantNestProtectData
from .const import CONF_REFRESH_TOKEN, DOMAIN

TO_REDACT = [
    "city",
    "state",
    "zip",
    "country",
    "service_config",
    "pairing_token",
    "access_token",
    "name",
    "location",
    "ifj_primary_fabric_id",
    "aux_primary_fabric_id",
    "topaz_hush_key",
    "postal_code",
    "latitude",
    "longitude",
    "thread_ip_address",
    "serial_number",
]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    refresh_token = entry.data[CONF_REFRESH_TOKEN]

    entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    client = entry_data.client

    auth = await client.get_access_token(refresh_token)
    nest = await client.authenticate(auth.access_token)

    data = {"app_launch": await client.get_first_data(nest.access_token, nest.userid)}

    return async_redact_data(data, TO_REDACT)


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a device entry."""
    refresh_token = entry.data[CONF_REFRESH_TOKEN]

    entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    client = entry_data.client

    auth = await client.get_access_token(refresh_token)
    nest = await client.authenticate(auth.access_token)

    data = {
        "device": {
            "controllable_name": device.hw_version,
            "firmware": device.sw_version,
            "model": device.model,
        },
        "app_launch": await client.get_first_data(nest.access_token, nest.userid),
    }

    return async_redact_data(data, TO_REDACT)
