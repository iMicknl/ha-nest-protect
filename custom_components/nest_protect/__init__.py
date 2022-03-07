"""Nest Protect integration."""
import asyncio
from dataclasses import dataclass
from typing import Any

from aiohttp import ServerDisconnectedError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import CONF_REFRESH_TOKEN, DOMAIN, LOGGER, PLATFORMS
from .pynest.client import NestClient
from .pynest.exceptions import NotAuthenticatedException, PynestException
from .pynest.models import Bucket, TopazBucket


@dataclass
class HomeAssistantNestProtectData:
    """Nest Protect data stored in the Home Assistant data object."""

    devices: dict[str, Bucket]
    areas: list[str, str]
    client: NestClient


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Nest Protect from a config entry."""
    refresh_token = entry.data[CONF_REFRESH_TOKEN]

    if not refresh_token:
        raise ConfigEntryNotReady("No refresh token provided")

    session = async_get_clientsession(hass)
    client = NestClient(session)

    try:
        auth = await client.get_access_token(refresh_token)
        nest = await client.authenticate(auth.access_token)
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

        # Temperature Sensors
        if key.startswith("kryptonite."):
            kryptonite = Bucket(**bucket)
            LOGGER.debug("Detected temperature sensor")
            LOGGER.debug(kryptonite)
            devices.append(kryptonite)

    devices: dict[str, Bucket] = {b.object_key: b for b in devices}

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = HomeAssistantNestProtectData(
        devices=devices,
        areas=areas,
        client=client,
    )

    hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    # Subscribe for real-time updates
    # TODO cancel when closing HA / unloading entry
    _register_subscribe_task(hass, entry, data)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


def _register_subscribe_task(hass: HomeAssistant, entry: ConfigEntry, data: Any):
    return asyncio.create_task(_async_subscribe_for_data(hass, entry, data))


async def _async_subscribe_for_data(hass: HomeAssistant, entry: ConfigEntry, data: Any):
    """Subscribe for new data."""
    entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]

    LOGGER.debug("Subscriber: listening for new data")

    try:
        # TODO move refresh token logic to client
        if (
            not entry_data.client.nest_session
            or entry_data.client.nest_session.is_expired()
        ):
            LOGGER.debug("Subscriber: authenticate for new Nest session")

            if not entry_data.client.auth or entry_data.client.auth.is_expired():
                LOGGER.debug("Subscriber: retrieving new Google access token")
                await entry_data.client.get_access_token()

            await entry_data.client.authenticate(entry_data.client.auth.access_token)

        # Subscribe to Google Nest subscribe endpoint
        result = await entry_data.client.subscribe_for_data(
            entry_data.client.nest_session.access_token,
            entry_data.client.nest_session.userid,
            data["service_urls"]["urls"]["transport_url"],
            data["updated_buckets"],
        )

        # TODO write this data away in a better way, best would be to directly model API responses in client
        for bucket in result["objects"]:
            key = bucket["object_key"]

            # Nest Protect
            if key.startswith("topaz."):
                topaz = TopazBucket(**bucket)
                entry_data.devices[key] = topaz

                # TODO investigate if we want to use dispatcher, or get data from entry data in sensors
                async_dispatcher_send(hass, key, topaz)

            # Areas
            if key.startswith("where."):
                bucket_value = Bucket(**bucket).value

                for area in bucket_value["wheres"]:
                    entry_data.areas[area["where_id"]] = area["name"]

        # Update buckets with new data, to only receive new updates
        buckets = {d["object_key"]: d for d in result["objects"]}
        objects = [
            dict(d, **buckets.get(d["object_key"], {})) for d in data["updated_buckets"]
        ]

        data["updated_buckets"] = objects

        _register_subscribe_task(hass, entry, data)
    except ServerDisconnectedError:
        LOGGER.debug("Subscriber: server disconnected.")
        _register_subscribe_task(hass, entry, data)

    except asyncio.exceptions.TimeoutError:
        LOGGER.debug("Subscriber: session timed out.")
        _register_subscribe_task(hass, entry, data)

    except NotAuthenticatedException:
        LOGGER.debug("Subscriber: 401 exception.")
        # Renewing access token
        await entry_data.client.get_access_token()
        await entry_data.client.authenticate(entry_data.client.auth.access_token)
        _register_subscribe_task(hass, entry, data)

    except PynestException:
        LOGGER.exception("Subscriber: pynest exception.")

        # Wait a minute before retrying
        await asyncio.sleep(60)
        _register_subscribe_task(hass, entry, data)

    except Exception:  # pylint: disable=broad-except
        LOGGER.exception(
            "Unknown exception. Live updates disabled. Please turn on debug mode and create an issue on GitHub."
        )
