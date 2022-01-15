"""Nest Protect integration."""
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, LOGGER, PLATFORMS
from .coordinator import NestProtectDataUpdateCoordinator
from .pynest.client import NestClient
from .pynest.models import Bucket, TopazBucket

SCAN_INTERVAL = timedelta(seconds=30)


@dataclass
class HomeAssistantNestProtectData:
    """Nest Protect data stored in the Home Assistant data object."""

    coordinator: NestProtectDataUpdateCoordinator


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

    coordinator = NestProtectDataUpdateCoordinator(
        hass,
        name="events",
        client=client,
        update_interval=timedelta(seconds=30),
        devices=devices,
        areas=areas,
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = HomeAssistantNestProtectData(
        coordinator=coordinator
    )

    hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok