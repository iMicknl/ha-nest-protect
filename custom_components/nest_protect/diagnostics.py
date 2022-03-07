"""Provides diagnostics for Nest Protect."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import HomeAssistantNestProtectData
from .const import CONF_REFRESH_TOKEN, DOMAIN


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

    return data


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

    return data
