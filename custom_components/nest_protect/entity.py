"""Entity class for Nest Protect."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiohttp import ClientError
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityDescription

from .const import ATTRIBUTION, DOMAIN
from .pynest.client import NestClient
from .pynest.exceptions import (
    BadCredentialsException,
    NestServiceException,
    NotAuthenticatedException,
    PynestException,
)
from .pynest.models import Bucket

if TYPE_CHECKING:
    from .session import NestSessionManager
    from .stream_health import StreamHealth


async def async_run_entity_command[T](
    entity: Entity,
    session_manager: NestSessionManager,
    command: Callable[[], Awaitable[T]],
    auth_errors: type[Exception] | tuple[type[Exception], ...],
    failure: str,
) -> T:
    """Run a Nest command for an entity, reporting failures as Home Assistant errors."""
    try:
        return await session_manager.async_run_command(command, auth_errors)
    except BadCredentialsException as err:
        # Google rejected the refresh, so only new credentials can help.
        entry = entity.platform.config_entry
        entry.runtime_data.stream_health.async_mark_unavailable(
            "Re-authentication required"
        )
        entry.async_start_reauth(entity.hass)
        raise HomeAssistantError(f"{failure}: re-authentication required") from err
    except (
        ClientError,
        TimeoutError,
        NestServiceException,
        NotAuthenticatedException,
        PynestException,
    ) as err:
        raise HomeAssistantError(f"{failure}: {err}") from err


class NestEntity(Entity):
    """Class to describe an Nest entity and link it to a device."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # Entities reporting the device's own connectivity set this to False, so
    # that state stays visible while other values are considered stale.
    _follows_stream_health = True
    _stream_health: StreamHealth | None = None

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
        self.area = areas.get(self.bucket.value.get("where_id"))

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

            # Absent means unknown, not battery-powered
            wired_or_battery = self.bucket.value.get("wired_or_battery")
            if wired_or_battery is None:
                hw_version = None
            else:
                hw_version = "Wired" if wired_or_battery == 0 else "Battery"

            return DeviceInfo(
                connections=connections,
                identifiers={(DOMAIN, identifier)},
                name=f"Nest Protect ({label})" if label else "Nest Protect",
                manufacturer="Google",
                model=self.bucket.value.get("model"),
                sw_version=self.bucket.value.get("software_version"),
                hw_version=hw_version,
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

        return None

    @property
    def available(self) -> bool:
        """Return False when updates from Nest have stopped arriving."""
        if self._stream_health is not None and not self._stream_health.available:
            return False
        return super().available

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to register update signal handler."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.bucket.object_key, self.update_callback
            )
        )

        if self._follows_stream_health:
            entry_data = self.platform.config_entry.runtime_data
            self._stream_health = entry_data.stream_health
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass, self._stream_health.signal, self.async_write_ha_state
                )
            )

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place entity_id changes.

        Signals are keyed on Nest buckets, not on the entity_id, so nothing
        needs to be subscribed again. Without this hook HA removes and re-adds
        the entity instead.
        """
        super().async_entity_id_changed(old_entity_id)

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


class NestHomeEntity(NestDescriptiveEntity):
    """Entity that belongs to a Nest home (structure) instead of a device."""

    def __init__(
        self,
        bucket: Bucket,
        description: EntityDescription,
        home_name: str | None,
        client: NestClient,
    ) -> None:
        """Initialize the home entity."""
        # Set before super().__init__(), which builds the device info.
        self.home_name = home_name
        super().__init__(bucket, description, {}, client)

    def generate_device_info(self) -> DeviceInfo:
        """Generate device info for the home."""
        structure_id = self.bucket.object_key.split(".", 1)[1]

        return DeviceInfo(
            identifiers={(DOMAIN, f"structure.{structure_id}")},
            name=self.home_name or "Nest Home",
            manufacturer="Google",
        )


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
        return await async_run_entity_command(
            self,
            self.session_manager,
            # Re-read the session on each attempt to pick up a refreshed token.
            lambda: self.client.update_objects(
                self.client.nest_session.access_token,
                self.client.nest_session.userid,
                self.client.transport_url,
                objects,
            ),
            NotAuthenticatedException,
            f"Failed to update {self.entity_id}",
        )
