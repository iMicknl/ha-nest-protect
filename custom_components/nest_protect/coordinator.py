"""Coordinator for Nest Protect."""

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from custom_components.nest_protect.pynest.client import NestClient
from custom_components.nest_protect.pynest.models import Bucket

from .const import LOGGER


class NestProtectDataUpdateCoordinator(DataUpdateCoordinator[list[Bucket]]):
    """Class to manage fetching data from the API."""

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        client: NestClient,
        update_interval: timedelta,
        devices: list[Bucket],
        areas: list,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            LOGGER,
            name=name,
            update_interval=update_interval,
        )

        self.api = client
        self.platforms = []
        self.areas = areas
        self.devices: dict[str, Bucket] = {b.object_key: b for b in devices}

    async def _async_update_data(self):
        """Update data."""
        try:
            # TODO Subscribe to Google Nest pubsub event / subscribe endpoint

            # TODO Pull new buckets and return this here
            # TODO Needs change in entity.py
            return self.devices
        except Exception as exception:
            raise UpdateFailed() from exception
