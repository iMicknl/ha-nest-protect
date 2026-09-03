"""Lock platform and battery sensor for Nest x Yale locks."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError
from homeassistant.components.lock import LockEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTRIBUTION, DOMAIN
from .pynest.exceptions import (
    BadCredentialsException,
    NestLockAuthException,
    NestServiceException,
    NotAuthenticatedException,
    PynestException,
)
from .pynest.grpc_client import GrpcLockClient
from .pynest.lock_models import LockBoltState, LockState

if TYPE_CHECKING:
    from .session import NestSessionManager

LOCK_SIGNAL_PREFIX = "nest_protect_lock_"


def _compose_lock_device_name(location: str | None, name: str) -> str:
    """Combine an optional room label with the lock's own name.

    Mirrors nest_legacy's `entity.py` formatting: "Front Door Lock". Falls
    back to "Nest x Yale Lock" when neither the location nor a meaningful
    label is available.
    """
    location = (location or "").strip()
    name = (name or "").strip()
    if location and location.lower() not in name.lower():
        return f"{location} {name}".strip()
    if name and name.lower() != "lock":
        return name
    return "Nest x Yale Lock"


def lock_signal(resource_id: str) -> str:
    """Dispatcher signal name for a single lock resource."""
    return f"{LOCK_SIGNAL_PREFIX}{resource_id}"


def discovery_signal(entry_id: str) -> str:
    """Dispatcher signal for newly-discovered locks on a config entry."""
    return f"{LOCK_SIGNAL_PREFIX}discover_{entry_id}"


def subscribe_to_lock_discovery(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[LockState], Entity],
) -> None:
    """Wire one `async_add_entities` callback into per-resource lock discovery.

    `factory(lock_state)` builds one entity per newly-seen resource_id. Locks
    already cached on the config entry (from observe events that arrived before
    this platform set up) are replayed immediately.
    """
    entry_data = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _on_locks_discovered(locks: dict[str, LockState]) -> None:
        new_entities: list[Entity] = []
        for resource_id, lock_state in locks.items():
            if resource_id in known:
                continue
            known.add(resource_id)
            new_entities.append(factory(lock_state))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, discovery_signal(entry.entry_id), _on_locks_discovered
        )
    )

    if entry_data.lock_state_cache:
        _on_locks_discovered(entry_data.lock_state_cache)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up lock entities. Discovery is event-driven via dispatcher."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    grpc_client = entry_data.grpc_lock_client
    subscribe_to_lock_discovery(
        hass,
        entry,
        async_add_entities,
        lambda state: NestLockEntity(grpc_client, state, entry_data.session_manager),
    )


class NestLockEntity(LockEntity):
    """Single Nest x Yale lock — state + lock/unlock commands."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = None  # The lock is the primary feature of the device

    def __init__(
        self,
        grpc_client: GrpcLockClient,
        lock_state: LockState,
        session_manager: NestSessionManager,
    ) -> None:
        """Initialize."""
        self._grpc_client = grpc_client
        self._lock_state = lock_state
        self._session_manager = session_manager
        self._attr_unique_id = f"lock_{lock_state.resource_id}"
        self._attr_attribution = ATTRIBUTION
        self._attr_device_info = self._build_device_info()

    def _build_device_info(self) -> DeviceInfo:
        """Construct DeviceInfo from the current LockState."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._lock_state.serial_number)},
            name=_compose_lock_device_name(
                self._lock_state.location, self._lock_state.name
            ),
            manufacturer="Google",
            model="Nest x Yale Lock",
            sw_version=self._lock_state.software_version,
            suggested_area=self._lock_state.location,
        )

    @property
    def is_locked(self) -> bool | None:
        """Return true if locked, false if unlocked, None if unknown."""
        state = self._lock_state.bolt_state
        if state == LockBoltState.LOCKED:
            return True
        if state == LockBoltState.UNLOCKED:
            return False
        return None

    @property
    def is_locking(self) -> bool:
        """Return true if the bolt is extending."""
        return self._lock_state.bolt_state == LockBoltState.LOCKING

    @property
    def is_unlocking(self) -> bool:
        """Return true if the bolt is retracting."""
        return self._lock_state.bolt_state == LockBoltState.UNLOCKING

    @property
    def is_jammed(self) -> bool:
        """Return true if the bolt is jammed."""
        return self._lock_state.bolt_state == LockBoltState.JAMMED

    async def async_added_to_hass(self) -> None:
        """Subscribe to state updates for this lock."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                lock_signal(self._lock_state.resource_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self, lock_state: LockState) -> None:
        """Receive a new LockState from the observe loop."""
        previous = self._lock_state
        self._lock_state = lock_state
        # DeviceInfo is only read when the entity is added, so a location or
        # sw_version that arrives later has to be written to the registry
        # directly. Locks get their room label from the structure-level
        # annotation traits, which routinely arrive after the lock itself.
        if (
            lock_state.location != previous.location
            or lock_state.software_version != previous.software_version
        ) and (device_id := self.device_entry and self.device_entry.id):
            dr.async_get(self.hass).async_update_device(
                device_id,
                name=_compose_lock_device_name(lock_state.location, lock_state.name),
                sw_version=lock_state.software_version,
            )
        self.async_write_ha_state()

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the lock."""
        try:
            await self._async_send_command(lock=True)
        except (
            ClientError,
            TimeoutError,
            BadCredentialsException,
            NestServiceException,
            NotAuthenticatedException,
            PynestException,
        ) as err:
            raise HomeAssistantError(f"Failed to lock: {err}") from err

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the lock."""
        try:
            await self._async_send_command(lock=False)
        except (
            ClientError,
            TimeoutError,
            BadCredentialsException,
            NestServiceException,
            NotAuthenticatedException,
            PynestException,
        ) as err:
            raise HomeAssistantError(f"Failed to unlock: {err}") from err

    async def _async_send_command(self, *, lock: bool) -> None:
        """Send a command and retry once after coordinated session recovery."""
        await self._session_manager.ensure_session()
        rejected_session = self._session_manager.current_session

        try:
            await self._grpc_client.send_lock_command(
                self._lock_state.resource_id,
                lock=lock,
                nest_session=rejected_session,
            )
        except NestLockAuthException:
            await self._session_manager.async_refresh_session(
                rejected_session=rejected_session
            )
            replacement_session = self._session_manager.current_session
            try:
                await self._grpc_client.send_lock_command(
                    self._lock_state.resource_id,
                    lock=lock,
                    nest_session=replacement_session,
                )
            except NestLockAuthException:
                if replacement_session is not None:
                    await self._session_manager.async_invalidate_session(
                        rejected_session=replacement_session
                    )
                raise


class NestLockBatterySensor(SensorEntity):
    """Battery-percentage sensor for a Nest x Yale lock."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # No _attr_name / translation key: with has_entity_name and the BATTERY
    # device class, HA names this entity "Battery" on its own.
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, lock_state: LockState) -> None:
        """Initialize."""
        self._lock_state = lock_state
        self._attr_unique_id = f"lock_{lock_state.resource_id}_battery"
        self._attr_attribution = ATTRIBUTION
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, lock_state.serial_number)},
        )

    @property
    def native_value(self) -> float | None:
        """Return battery percent if known."""
        if self._lock_state.battery_level is None:
            return None
        return round(self._lock_state.battery_level, 1)

    async def async_added_to_hass(self) -> None:
        """Subscribe to lock-state updates for this resource."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                lock_signal(self._lock_state.resource_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self, lock_state: LockState) -> None:
        """Receive an updated LockState from the observe loop."""
        self._lock_state = lock_state
        self.async_write_ha_state()
