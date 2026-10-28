"""Binary sensor platform for Nest Protect."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NestProtectConfigEntry
from .entity import NestDescriptiveEntity
from .pynest.client import NestClient
from .pynest.enums import BucketType
from .pynest.models import Bucket

PARALLEL_UPDATES = 0


@dataclass
class NestProtectBinarySensorDescriptionMixin:
    """Define an entity description mixin for binary sensor entities."""

    value_fn: Callable[[Any], bool]


@dataclass
class NestProtectBinarySensorDescription(
    BinarySensorEntityDescription, NestProtectBinarySensorDescriptionMixin
):
    """Class to describe a Nest Protect binary sensor."""

    wired_only: bool = False


BINARY_SENSOR_DESCRIPTIONS: list[BinarySensorEntityDescription] = [
    NestProtectBinarySensorDescription(
        key="co_status",
        translation_key="co_status",
        device_class=BinarySensorDeviceClass.CO,
        value_fn=lambda state: state != 0,
    ),
    NestProtectBinarySensorDescription(
        key="smoke_status",
        translation_key="smoke_status",
        device_class=BinarySensorDeviceClass.SMOKE,
        value_fn=lambda state: state != 0,
    ),
    NestProtectBinarySensorDescription(
        key="heat_status",
        translation_key="heat_status",
        device_class=BinarySensorDeviceClass.HEAT,
        value_fn=lambda state: state != 0,
    ),
    NestProtectBinarySensorDescription(
        key="hushed_state",
        translation_key="hushed_state",
        icon="mdi:bell-sleep",
        value_fn=lambda state: state,
    ),
    NestProtectBinarySensorDescription(
        key="component_speaker_test_passed",
        translation_key="component_speaker_test_passed",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:speaker-wireless",
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="battery_health_state",
        translation_key="battery_health_state",
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state,
    ),
    NestProtectBinarySensorDescription(
        key="component_smoke_test_passed",
        translation_key="component_smoke_test_passed",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:smoke",
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="component_co_test_passed",
        translation_key="component_co_test_passed",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:molecule-co",
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="component_wifi_test_passed",
        translation_key="component_wifi_test_passed",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:wifi",
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="component_led_test_passed",
        translation_key="component_led_test_passed",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:led-off",
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="component_pir_test_passed",
        translation_key="component_pir_test_passed",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:run",
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="component_buzzer_test_passed",
        translation_key="component_buzzer_test_passed",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:alarm-bell",
        value_fn=lambda state: not state,
    ),
    # Disabled for now, since it seems like this state is not valid
    # NestProtectBinarySensorDescription(
    #     key="component_heat_test_passed",
    #     name="Heat Test",
    #     device_class=BinarySensorDeviceClass.PROBLEM,
    #     entity_category=EntityCategory.DIAGNOSTIC,
    #     icon="mdi:fire",
    #     value_fn=lambda state: not state
    # ),
    NestProtectBinarySensorDescription(
        key="component_hum_test_passed",
        translation_key="component_hum_test_passed",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:water-percent",
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="component_als_test_passed",
        translation_key="component_als_test_passed",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:brightness-6",
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="component_temp_test_passed",
        translation_key="component_temp_test_passed",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:thermometer",
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="removed_from_base",
        translation_key="removed_from_base",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:tray-remove",
        value_fn=lambda state: state,
    ),
    NestProtectBinarySensorDescription(
        key="auto_away",
        translation_key="auto_away",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        wired_only=True,
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="line_power_present",
        translation_key="line_power_present",
        device_class=BinarySensorDeviceClass.POWER,
        entity_category=EntityCategory.DIAGNOSTIC,
        wired_only=True,
        value_fn=lambda state: state,
    ),
]

# Not a topaz field: online state comes from the Protect's widget_track bucket.
CONNECTIVITY_DESCRIPTION = NestProtectBinarySensorDescription(
    key="is_online",
    translation_key="is_online",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
    value_fn=lambda state: state,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NestProtectConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Set up the Nest Protect binary sensors from a config entry."""

    data = entry.runtime_data
    entities: list[NestProtectBinarySensor] = []

    supported_keys: dict[str, NestProtectBinarySensorDescription] = {
        description.key: description for description in BINARY_SENSOR_DESCRIPTIONS
    }

    for device in data.devices.values():
        for key in device.value:
            if description := supported_keys.get(key):
                # Not all entities are useful for battery powered Nest Protect devices
                if description.wired_only and device.value.get("wired_or_battery") != 0:
                    continue

                entities.append(
                    NestProtectBinarySensor(
                        device, description, data.areas, data.client
                    )
                )

        if device.type == BucketType.TOPAZ:
            serial = device.object_key.removeprefix("topaz.")
            widget_track_key = f"widget_track.{serial}"
            entities.append(
                NestProtectConnectivitySensor(
                    device,
                    CONNECTIVITY_DESCRIPTION,
                    data.areas,
                    data.client,
                    widget_track_key=widget_track_key,
                    widget_track=data.widget_tracks.get(widget_track_key),
                )
            )

    async_add_devices(entities)


class NestProtectBinarySensor(NestDescriptiveEntity, BinarySensorEntity):
    """Representation of a Nest Protect Binary Sensor."""

    entity_description: NestProtectBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        """Return the state of the sensor."""
        state = self.bucket.value.get(self.entity_description.key)
        if state is None:
            return None
        return self.entity_description.value_fn(state)


class NestProtectConnectivitySensor(NestProtectBinarySensor):
    """Representation of a Nest Protect connectivity sensor."""

    def __init__(
        self,
        bucket: Bucket,
        description: NestProtectBinarySensorDescription,
        areas: dict[str, str],
        client: NestClient,
        *,
        widget_track_key: str,
        widget_track: Bucket | None,
    ) -> None:
        """Initialize the connectivity sensor."""
        super().__init__(bucket, description, areas, client)
        self.widget_track_key = widget_track_key
        self.widget_track = widget_track

    @property
    def available(self) -> bool:
        """Stay available so an offline Protect reports off, not unavailable."""
        return True

    @property
    def is_on(self) -> bool | None:
        """Return whether the Protect is online."""
        if self.widget_track is None:
            return None
        state = self.widget_track.value.get("online")
        if state is None:
            return None
        return self.entity_description.value_fn(state)

    async def async_added_to_hass(self) -> None:
        """Also follow widget_track updates for this Protect."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.widget_track_key, self.widget_track_callback
            )
        )

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place entity_id changes; signals don't use the entity_id."""
        super().async_entity_id_changed(old_entity_id)

    @callback
    def widget_track_callback(self, bucket: Bucket) -> None:
        """Update the connectivity state."""
        self.widget_track = bucket
        self.async_write_ha_state()
