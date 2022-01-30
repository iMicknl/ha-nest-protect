"""Nest Protect integration."""
import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, LOGGER, PLATFORMS
from .pynest.client import NestClient
from .pynest.models import Bucket, TopazBucket

SCAN_INTERVAL = timedelta(seconds=30)


@dataclass
class HomeAssistantNestProtectData:
    """Nest Protect data stored in the Home Assistant data object."""

    devices: dict[str, Bucket]
    areas: list[str, str]
    client: NestClient
    data_subscriber_task: asyncio.Task[Awaitable]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Nest Protect from a config entry."""
    refresh_token = entry.data["refresh_token"]

    if not refresh_token:
        raise ConfigEntryNotReady("No refresh token provided")

    session = async_get_clientsession(hass)
    client = NestClient(session)

    try:
        access_token = await client.get_access_token(refresh_token)
        nest = await client.authenticate(access_token)
    except Exception as exception:  # pylint: disable=broad-except
        LOGGER.exception(exception)
        raise ConfigEntryNotReady from exception

    # Get initial first data (move later to coordinator)
    data = await client.get_first_data(nest.access_token, nest.userid)

    devices: list[Bucket] = []
    areas: dict[str, str] = {}

    for bucket in data["updated_buckets"]:
        key = bucket["object_key"]

        # Nest Protect
        if key.startswith("topaz."):
            topaz = TopazBucket(**bucket)
            devices.append(topaz)

        # Areas
        if key.startswith("where."):
            bucket_value = Bucket(**bucket).value

            for area in bucket_value["wheres"]:
                areas[area["where_id"]] = area["name"]

        # Yale Locks
        if key.startswith("kryptonite."):
            kryptonite = Bucket(**bucket)
            LOGGER.debug("Detected lock")
            LOGGER.debug(kryptonite)
            devices.append(kryptonite)

    devices: dict[str, Bucket] = {b.object_key: b for b in devices}

    task_data_subscriber = hass.async_create_task(
        _async_subscribe_for_data(hass, entry, data)
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = HomeAssistantNestProtectData(
        devices=devices,
        areas=areas,
        client=client,
        data_subscriber_task=task_data_subscriber,
    )

    hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    # Unregister data subscriber
    entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]

    entry_data.data_subscriber_task()

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def _async_subscribe_for_data(hass: HomeAssistant, entry: ConfigEntry, data: Any):
    """Subscribe for new data."""
    entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]

    LOGGER.debug("Subscribing for data")

    try:
        access_token = await entry_data.client.get_access_token()
        nest = await entry_data.client.authenticate(access_token)

        # Subscribe to Google Nest subscribe endpoint
        result = await entry_data.client.subscribe_for_data(
            nest.access_token,
            nest.userid,
            data["service_urls"]["urls"]["transport_url"],
            data["updated_buckets"],
        )

        LOGGER.debug(result)

        # TODO write this data away in a better way
        for bucket in data["objects"]:
            key = bucket["object_key"]

            # Nest Protect
            if key.startswith("topaz."):
                topaz = TopazBucket(**bucket)
                entry_data.devices[key] = topaz

            # Areas
            if key.startswith("where."):
                bucket_value = Bucket(**bucket).value

                for area in bucket_value["wheres"]:
                    entry_data.areas[area["where_id"]] = area["name"]

    except asyncio.exceptions.TimeoutError:
        LOGGER.debug("Subscribe session timed out.")

    except Exception as exception:  # pylint: disable=broad-except
        LOGGER.exception(exception)

    finally:
        entry_data.data_subscriber_task = hass.async_create_task(
            _async_subscribe_for_data(hass, entry, data)
        )
