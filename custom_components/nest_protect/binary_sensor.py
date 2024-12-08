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
from homeassistant.helpers.entity import EntityCategory

from . import HomeAssistantNestProtectData
from .const import DOMAIN
from .entity import NestDescriptiveEntity


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
        name="CO Status",
        device_class=BinarySensorDeviceClass.CO,
        value_fn=lambda state: state != 0,
    ),
    NestProtectBinarySensorDescription(
        key="smoke_status",
        name="Smoke Status",
        device_class=BinarySensorDeviceClass.SMOKE,
        value_fn=lambda state: state != 0,
    ),
    NestProtectBinarySensorDescription(
        key="heat_status",
        name="Heat Status",
        device_class=BinarySensorDeviceClass.HEAT,
        value_fn=lambda state: state != 0,
    ),
    NestProtectBinarySensorDescription(
        key="component_speaker_test_passed",
        name="Speaker Test",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:speaker-wireless",
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="battery_health_state",
        name="Battery Health",
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state,
    ),
    NestProtectBinarySensorDescription(
        key="is_online",
        name="Online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state,
    ),
    NestProtectBinarySensorDescription(
        key="component_smoke_test_passed",
        name="Smoke Test",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:smoke",
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="component_co_test_passed",
        name="CO Test",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:molecule-co",
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="component_wifi_test_passed",
        name="WiFi Test",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:wifi",
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="component_led_test_passed",
        name="LED Test",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:led-off",
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="component_pir_test_passed",
        name="PIR Test",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:run",
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="component_buzzer_test_passed",
        name="Buzzer Test",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
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
        name="Humidity Test",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:water-percent",
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="removed_from_base",
        name="Removed from Base",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:tray-remove",
        value_fn=lambda state: state,
    ),
    NestProtectBinarySensorDescription(
        key="auto_away",
        name="Occupancy",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        wired_only=True,
        value_fn=lambda state: not state,
    ),
    NestProtectBinarySensorDescription(
        key="line_power_present",
        name="Line Power",
        device_class=BinarySensorDeviceClass.POWER,
        entity_category=EntityCategory.DIAGNOSTIC,
        wired_only=True,
        value_fn=lambda state: state,
    ),
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Set up the Nest Protect binary sensors from a config entry."""

    data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    entities: list[NestProtectBinarySensor] = []

    SUPPORTED_KEYS: dict[str, NestProtectBinarySensorDescription] = {
        description.key: description for description in BINARY_SENSOR_DESCRIPTIONS
    }

    for device in data.devices.values():
        for key in device.value:
            if description := SUPPORTED_KEYS.get(key):
                # Not all entities are useful for battery powered Nest Protect devices
                if description.wired_only and device.value["wired_or_battery"] != 0:
                    continue

                entities.append(
                    NestProtectBinarySensor(
                        device, description, data.areas, data.client
                    )
                )

    async_add_devices(entities)


class NestProtectBinarySensor(NestDescriptiveEntity, BinarySensorEntity):
    """Representation of a Nest Protect Binary Sensor."""

    entity_description: NestProtectBinarySensorDescription

    @property
    def is_on(self) -> bool:
        """Return the state of the sensor."""
        state = self.bucket.value.get(self.entity_description.key)
        return self.entity_description.value_fn(state)
