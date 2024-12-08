"""Entity class for Nest Protect."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity, EntityDescription

from .const import ATTRIBUTION, DOMAIN
from .pynest.client import NestClient
from .pynest.models import Bucket


class NestEntity(Entity):
    """Class to describe an Nest entity and link it to a device."""

    _attr_should_poll = False

    def __init__(
        self,
        bucket: Bucket,
        description: EntityDescription,
        areas: dict[str, str],
        client: NestClient,
    ):
        """Initialize."""
        self.entity_description = description
        self.bucket = bucket
        self.client = client
        self.area = areas.get(self.bucket.value["where_id"])

        self._attr_unique_id = bucket.object_key
        self._attr_attribution = ATTRIBUTION
        self._attr_name = self.device_name()
        self._attr_device_info = self.generate_device_info()

    def device_name(self) -> str | None:
        """Generate device name."""
        if label := self.bucket.value.get("description"):
            name = label
        elif self.area:
            name = self.area
        else:
            name = ""

        if self.bucket.object_key.startswith("topaz."):
            return f"Nest Protect ({name})"

        if self.bucket.object_key.startswith("kryptonite."):
            return f"Nest Temperature Sensor ({name})"

        return None

    def generate_device_info(self) -> DeviceInfo | None:
        """Generate device info."""

        if self.bucket.object_key.startswith("topaz."):
            return DeviceInfo(
                connections={
                    (dr.CONNECTION_NETWORK_MAC, self.bucket.value["wifi_mac_address"])
                },
                identifiers={(DOMAIN, self.bucket.value["serial_number"])},
                name=self._attr_name,
                manufacturer="Google",
                model=self.bucket.value["model"],
                sw_version=self.bucket.value["software_version"],
                hw_version=(
                    "Wired" if self.bucket.value["wired_or_battery"] == 0 else "Battery"
                ),
                suggested_area=self.area,
                configuration_url="https://home.nest.com/protect/"
                + self.bucket.value["structure_id"],  # TODO change url based on device
            )

        if self.bucket.object_key.startswith("kryptonite."):
            identifier = (
                self.bucket.value.get("serial_number") or self.bucket.object_key
            )

            return DeviceInfo(
                identifiers={(DOMAIN, identifier)},
                name=self._attr_name,
                manufacturer="Google",
                model=self.bucket.value.get("model"),
                suggested_area=self.area,
            )

        return None

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to register update signal handler."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.bucket.object_key, self.update_callback
            )
        )

    @callback
    def update_callback(self, bucket: Bucket):
        """Update the entities state."""

        self.bucket = bucket
        self.async_write_ha_state()


class NestDescriptiveEntity(NestEntity):
    """Class to describe an Nest entity which uses a Entity Description."""

    def __init__(
        self,
        bucket: Bucket,
        description: EntityDescription,
        areas: dict[str, str],
        client: NestClient,
    ) -> None:
        """Initialize the device."""
        super().__init__(bucket, description, areas, client)
        self._attr_name = f"{super().name} {self.entity_description.name}"
        self._attr_unique_id = f"{super().unique_id}-{self.entity_description.key}"
