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
        value_fn=lambda state: state == 3,
    ),
    NestProtectBinarySensorDescription(
        key="smoke_status",
        name="Smoke Status",
        device_class=BinarySensorDeviceClass.SMOKE,
        value_fn=lambda state: state == 3,
    ),
    NestProtectBinarySensorDescription(
        key="heat_status",
        name="Heat Status",
        device_class=BinarySensorDeviceClass.HEAT,
        value_fn=lambda state: state == 3,
    ),
    NestProtectBinarySensorDescription(
        key="component_speaker_test_passed",
        name="Speaker Test",
        value_fn=lambda state: not state,
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:speaker-wireless",
    ),
    NestProtectBinarySensorDescription(
        key="battery_health_state",
        name="Battery Health",
        value_fn=lambda state: state,
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    NestProtectBinarySensorDescription(
        key="component_wifi_test_passed",
        name="Online",
        value_fn=lambda state: state,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    NestProtectBinarySensorDescription(
        name="Smoke Test",
        key="component_smoke_test_passed",
        value_fn=lambda state: not state,
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:smoke",
    ),
    NestProtectBinarySensorDescription(
        name="CO Test",
        key="component_co_test_passed",
        value_fn=lambda state: not state,
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:molecule-co",
    ),
    NestProtectBinarySensorDescription(
        name="WiFi Test",
        key="component_wifi_test_passed",
        value_fn=lambda state: not state,
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:wifi",
    ),
    NestProtectBinarySensorDescription(
        name="LED Test",
        key="component_led_test_passed",
        value_fn=lambda state: not state,
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:led-off",
    ),
    NestProtectBinarySensorDescription(
        name="PIR Test",
        key="component_pir_test_passed",
        value_fn=lambda state: not state,
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:run",
    ),
    NestProtectBinarySensorDescription(
        name="Buzzer Test",
        key="component_buzzer_test_passed",
        value_fn=lambda state: not state,
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:alarm-bell",
    ),
    # Disabled for now, since it seems like this state is not valid
    # NestProtectBinarySensorDescription(
    #     name="Heat Test",
    #     key="component_heat_test_passed",
    #     value_fn=lambda state: not state,
    #     device_class=BinarySensorDeviceClass.PROBLEM,
    #     entity_category=EntityCategory.DIAGNOSTIC,
    #     icon="mdi:fire",
    # ),
    NestProtectBinarySensorDescription(
        name="Humidity Test",
        key="component_hum_test_passed",
        value_fn=lambda state: not state,
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:water-percent",
    ),
    NestProtectBinarySensorDescription(
        name="Occupancy",
        key="auto_away",
        value_fn=lambda state: not state,
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        wired_only=True,
    ),
    NestProtectBinarySensorDescription(
        name="Line Power",
        key="line_power_present",
        value_fn=lambda state: state,
        device_class=BinarySensorDeviceClass.POWER,
        entity_category=EntityCategory.DIAGNOSTIC,
        wired_only=True,
    ),
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Set up the Nest Protect binary sensors from a config entry."""

    data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    entities: list[NestProtectBinarySensor] = []

    SUPPORTED_KEYS = {
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
