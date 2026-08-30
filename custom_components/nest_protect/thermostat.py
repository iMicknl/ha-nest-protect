"""Discovery helpers for Nest thermostats seen on the protobuf observe stream.

Thermostats have no legacy REST bucket here — `NEST_REQUEST` never asks for the
`device`/`shared` types — so they are discovered from the protobuf stream after
the platforms have already been set up. This mirrors the dispatcher-based
discovery `lock.py` uses for Nest x Yale locks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .pynest.models import Bucket

THERMOSTAT_SIGNAL_PREFIX = "nest_protect_thermostat_"
THERMOSTAT_BUCKET_PREFIX = "device."

# Identity worth caching across reloads. Readings are deliberately excluded:
# they arrive within seconds of the stream reconnecting, and a restored
# temperature would be indistinguishable from a live one.
THERMOSTAT_CACHE_KEYS = frozenset(
    {
        "using_protobuf",
        "device_id",
        "structure_id",
        "protobuf_device_type",
        "serial_number",
        "model",
        "current_version",
        "where_id",
    }
)


def thermostat_cache_entry(bucket: Bucket) -> dict:
    """Reduce a thermostat bucket to the fields worth persisting."""
    return {
        key: value
        for key, value in bucket.value.items()
        if key in THERMOSTAT_CACHE_KEYS
    }


def thermostat_discovery_signal(entry_id: str) -> str:
    """Dispatcher signal for newly-discovered thermostats on a config entry."""
    return f"{THERMOSTAT_SIGNAL_PREFIX}discover_{entry_id}"


def is_discoverable_thermostat(bucket: Bucket) -> bool:
    """Check whether a bucket is a thermostat that is ready to be added.

    The serial number arrives with `DeviceIdentityTrait`, a moment after the
    `PeerDevicesTrait` that first reveals the device. Waiting for it means the
    Home Assistant device is registered with its real identifier, model and room
    rather than a placeholder, since `DeviceInfo` is only read once.
    """
    return bucket.object_key.startswith(THERMOSTAT_BUCKET_PREFIX) and bool(
        bucket.value.get("serial_number")
    )


def subscribe_to_thermostat_discovery(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[Bucket], Iterable[Entity]],
) -> None:
    """Wire one `async_add_entities` callback into thermostat discovery.

    `factory(bucket)` builds the entities for one newly-seen thermostat.
    Thermostats already discovered before this platform set up are replayed
    immediately.
    """
    entry_data = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _on_thermostat_discovered(bucket: Bucket) -> None:
        if bucket.object_key in known:
            return
        known.add(bucket.object_key)
        if new_entities := list(factory(bucket)):
            async_add_entities(new_entities)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, thermostat_discovery_signal(entry.entry_id), _on_thermostat_discovered
        )
    )

    for bucket in list(entry_data.devices.values()):
        if is_discoverable_thermostat(bucket):
            _on_thermostat_discovered(bucket)
