"""Entity class for Nest Protect."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity, EntityDescription

from .const import ATTRIBUTION, DOMAIN
from .pynest.client import NestClient
from .pynest.models import Bucket

if TYPE_CHECKING:
    from .session import NestSessionManager


class NestEntity(Entity):
    """Class to describe an Nest entity and link it to a device."""

    _attr_has_entity_name = True
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
        self._attr_device_info = self.generate_device_info()

    def _device_label(self) -> str:
        """Generate device label from description or area."""
        if label := self.bucket.value.get("description"):
            return label
        if self.area:
            return self.area
        return ""

    def generate_device_info(self) -> DeviceInfo | None:
        """Generate device info."""
        label = self._device_label()

        if self.bucket.object_key.startswith("topaz."):
            connections = set()
            mac = self.bucket.value.get("wifi_mac_address")
            if mac:
                connections.add((dr.CONNECTION_NETWORK_MAC, mac))

            identifier = (
                self.bucket.value.get("serial_number") or self.bucket.object_key
            )

            structure_id = self.bucket.value.get("structure_id")
            device_id = self.bucket.object_key.removeprefix("topaz.")

            return DeviceInfo(
                connections=connections,
                identifiers={(DOMAIN, identifier)},
                name=f"Nest Protect ({label})" if label else "Nest Protect",
                manufacturer="Google",
                model=self.bucket.value.get("model"),
                sw_version=self.bucket.value.get("software_version"),
                hw_version=(
                    "Wired"
                    if self.bucket.value.get("wired_or_battery") == 0
                    else "Battery"
                ),
                suggested_area=self.area,
                configuration_url=(
                    f"https://home.nest.com/protect/{structure_id}/settings/device/{device_id}#about"
                    if structure_id
                    else None
                ),
            )

        if self.bucket.object_key.startswith("kryptonite."):
            identifier = (
                self.bucket.value.get("serial_number") or self.bucket.object_key
            )

            return DeviceInfo(
                identifiers={(DOMAIN, identifier)},
                name=f"Nest Temperature Sensor ({label})"
                if label
                else "Nest Temperature Sensor",
                manufacturer="Google",
                model=self.bucket.value.get("model"),
                suggested_area=self.area,
            )

        if self.bucket.object_key.startswith("kryptonite."):
            identifier = (
                self.bucket.value.get("serial_number") or self.bucket.object_key
            )

            return DeviceInfo(
                identifiers={(DOMAIN, identifier)},
                name=f"Nest Temperature Sensor ({label})"
                if label
                else "Nest Temperature Sensor",
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
        self._attr_unique_id = f"{super().unique_id}-{self.entity_description.key}"


class NestUpdatableEntity(NestDescriptiveEntity):
    """Entity that can push state updates to Nest with session management."""

    def __init__(
        self,
        bucket: Bucket,
        description: EntityDescription,
        areas: dict[str, str],
        client: NestClient,
        session_manager: NestSessionManager,
    ) -> None:
        """Initialize the updatable entity."""
        super().__init__(bucket, description, areas, client)
        self.session_manager = session_manager

    async def _async_update_objects(self, objects: list[dict]) -> dict:
        """Update objects with automatic session refresh."""
        await self.session_manager.ensure_session()
        return await self.client.update_objects(
            self.client.nest_session.access_token,
            self.client.nest_session.userid,
            self.client.transport_url,
            objects,
        )
