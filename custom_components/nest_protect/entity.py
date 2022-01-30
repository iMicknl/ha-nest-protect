"""Entity class for Nest Protect."""
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity, EntityDescription

from .const import ATTRIBUTION, DOMAIN
from .pynest.models import Bucket


class NestEntity(Entity):
    """Class to describe an Nest entity and link it to a device."""

    _attr_should_poll = False

    def __init__(
        self, bucket: Bucket, description: EntityDescription, areas: dict[str, str]
    ):
        """Initialize."""
        self.entity_description = description
        self.bucket = bucket
        self._attr_unique_id = bucket.object_key
        self.area = areas[self.bucket.value.get("where_id")]

        if label := self.bucket.value.get("description"):
            self._attr_name = label
        else:
            self._attr_name = f"Nest Protect ({self.area})"

        self._attr_device_info = self.generate_device_info()

    def generate_device_info(self) -> DeviceInfo:
        """Generate device info."""

        # TODO make this less specific, currently mainly for Topaz / (nest device)
        # TODO change .get() to direct [""] access
        return DeviceInfo(
            connections={
                (dr.CONNECTION_NETWORK_MAC, self.bucket.value.get("wifi_mac_address"))
            },
            identifiers={(DOMAIN, self.bucket.value.get("serial_number"))},
            name=self._attr_name,
            manufacturer="Google",
            model=self.bucket.value.get("model"),
            sw_version=self.bucket.value.get("kl_software_version"),
            hw_version="Wired"
            if self.bucket.value.get("wired_or_battery") == 0
            else "Battery",
            suggested_area=self.area,
            configuration_url="https://home.nest.com/protect/"
            + self.bucket.value.get("structure_id"),  # TODO change url based on device
        )

    @property
    def extra_state_attributes(self) -> dict:
        """Return the state attributes."""
        return {"attribution": ATTRIBUTION}

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
        self, bucket: Bucket, description: EntityDescription, areas: dict[str, str]
    ) -> None:
        """Initialize the device."""
        super().__init__(bucket, description, areas)
        self._attr_name = f"{super().name} {self.entity_description.name}"
        self._attr_unique_id = f"{super().unique_id}-{self.entity_description.key}"
