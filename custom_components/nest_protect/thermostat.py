"""Read-only entities for Nest thermostats seen on the gRPC observe stream.

Thermostats have no REST bucket here, so they are discovered from the observe
stream after the platforms have been set up, like Nest x Yale locks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTRIBUTION, DOMAIN
from .entity import subscribe_to_discovery
from .pynest.thermostat_models import ThermostatState

if TYPE_CHECKING:
    from . import HomeAssistantNestProtectData, NestProtectConfigEntry

THERMOSTAT_SIGNAL_PREFIX = "nest_protect_thermostat_"


def thermostat_signal(resource_id: str) -> str:
    """Dispatcher signal name for a single thermostat resource."""
    return f"{THERMOSTAT_SIGNAL_PREFIX}{resource_id}"


def thermostat_discovery_signal(entry_id: str) -> str:
    """Dispatcher signal for newly-discovered thermostats on a config entry."""
    return f"{THERMOSTAT_SIGNAL_PREFIX}discover_{entry_id}"


def observer_stopped_signal(entry_id: str) -> str:
    """Dispatcher signal sent when a config entry's observe loop gives up."""
    return f"{DOMAIN}_observer_stopped_{entry_id}"


def subscribe_to_thermostat_discovery(
    hass: HomeAssistant,
    entry: NestProtectConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[ThermostatState], Iterable[Entity]],
) -> None:
    """Wire one `async_add_entities` callback into thermostat discovery."""
    subscribe_to_discovery(
        hass,
        entry,
        signal=thermostat_discovery_signal(entry.entry_id),
        cache=entry.runtime_data.thermostat_state_cache,
        async_add_entities=async_add_entities,
        factory=factory,
    )


def _device_name(state: ThermostatState) -> str:
    label = state.name or state.location
    return f"Nest Thermostat ({label})" if label else "Nest Thermostat"


class NestThermostatEntity(Entity):
    """Entity that belongs to a Nest thermostat."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _entry_data: HomeAssistantNestProtectData | None = None

    def __init__(self, state: ThermostatState, description: EntityDescription) -> None:
        """Initialize."""
        self.entity_description = description
        self._state = state
        self._attr_unique_id = f"thermostat_{state.resource_id}_{description.key}"
        self._attr_attribution = ATTRIBUTION
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, state.serial_number)},
            name=_device_name(state),
            manufacturer="Google",
            model=state.model,
            sw_version=state.software_version,
            suggested_area=state.location,
        )

    @property
    def available(self) -> bool:
        """Return False once the observe stream, the only source, has stopped."""
        if self._entry_data is not None and not self._entry_data.observing:
            return False
        return super().available

    async def async_added_to_hass(self) -> None:
        """Subscribe to state updates for this thermostat."""
        self._entry_data = self.platform.config_entry.runtime_data
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                thermostat_signal(self._state.resource_id),
                self._handle_update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                observer_stopped_signal(self.platform.config_entry.entry_id),
                self.async_write_ha_state,
            )
        )

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place entity_id changes; signals don't use the entity_id."""
        super().async_entity_id_changed(old_entity_id)

    @callback
    def _handle_update(self, state: ThermostatState) -> None:
        """Receive a new ThermostatState from the observe loop."""
        previous = self._state
        self._state = state
        # DeviceInfo is only read when the entity is added, so a name, room or
        # sw_version that arrives later has to be written to the registry
        # directly. The room label comes from structure-level annotation traits,
        # which routinely arrive after the thermostat itself.
        if (
            _device_name(state) != _device_name(previous)
            or state.software_version != previous.software_version
        ) and (device_id := self.device_entry and self.device_entry.id):
            dr.async_get(self.hass).async_update_device(
                device_id,
                name=_device_name(state),
                sw_version=state.software_version,
            )
        self.async_write_ha_state()
