"""Entity class for Nest Protect."""
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo, EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN
from .coordinator import NestProtectDataUpdateCoordinator
from .pynest.models import Bucket


class NestEntity(CoordinatorEntity):
    """Class to describe an Nest entity and link it to a device."""

    def __init__(
        self,
        device: Bucket,
        coordinator: NestProtectDataUpdateCoordinator,
        description: EntityDescription,
    ):
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self.device = device

        self._attr_unique_id = self.device.object_key

        if label := self.device.value.get("description"):
            self._attr_name = label
        else:
            area = self.coordinator.areas[self.device.value.get("where_id")]
            self._attr_name = f"Nest Protect ({area})"

        self._attr_device_info = self.generate_device_info()

    def generate_device_info(self) -> DeviceInfo:
        """Generate device info."""
        area = self.coordinator.areas[self.device.value.get("where_id")]

        # TODO make this less specific, currently mainly for Topaz / (nest device)
        return DeviceInfo(
            connections={
                (dr.CONNECTION_NETWORK_MAC, self.device.value.get("wifi_mac_address"))
            },
            identifiers={(DOMAIN, self.device.value.get("serial_number"))},
            name=self._attr_name,
            manufacturer="Google",
            model=self.device.value.get("model"),
            sw_version=self.device.value.get("kl_software_version"),
            hw_version="Wired"
            if self.device.value.get("wired_or_battery") == 0
            else "Battery",
            suggested_area=area,
            configuration_url="https://home.nest.com/protect/"
            + self.device.value.get("structure_id"),  # TODO change url based on device
        )

    @property
    def extra_state_attributes(self) -> dict:
        """Return the state attributes."""
        return {"attribution": ATTRIBUTION}


class NestDescriptiveEntity(NestEntity):
    """Class to describe an Nest entity which uses a Entity Description."""

    def __init__(
        self,
        device: Bucket,
        coordinator: NestProtectDataUpdateCoordinator,
        description: EntityDescription,
    ) -> None:
        """Initialize the device."""
        super().__init__(device, coordinator, description)
        self._attr_name = f"{super().name} {self.entity_description.name}"
        self._attr_unique_id = f"{super().unique_id}-{self.entity_description.key}"
