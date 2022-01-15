"""Sensor platform for Nest Protect."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import ENTITY_CATEGORY_CONFIG, ENTITY_CATEGORY_DIAGNOSTIC

from . import HomeAssistantNestProtectData
from .const import DOMAIN
from .entity import NestDescriptiveEntity


@dataclass
class NestProtectSensorDescriptionMixin:
    """Define an entity description mixin for binary sensor entities."""

    value_fn: Callable[[Any], bool]


@dataclass
class NestProtectSensorDescription(
    SensorEntityDescription, NestProtectSensorDescriptionMixin
):
    """Class to describe an Nest Protect sensor."""


SENSOR_DESCRIPTIONS: list[SensorEntityDescription] = [
    # TODO figure out Battery Level mapping
    # NestProtectSensorDescription(
    #     key="battery_level",
    #     name="Battery Level",
    #     value_fn=lambda state: state,
    # ),
    NestProtectSensorDescription(
        name="Replace By",
        key="replace_by_date_utc_secs",
        value_fn=lambda state: datetime.datetime.utcfromtimestamp(state),
        device_class=SensorDeviceClass.DATE,
        entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
    ),
    NestProtectSensorDescription(
        name="Pathlight",
        key="night_light_brightness",
        value_fn=lambda state: {1: "Off", 2: "On", 3: "Always On"}.get(state),
        entity_category=ENTITY_CATEGORY_CONFIG,
    ),
    # TODO Smoke Status (OK, Warning, Emergency)
    # TODO CO Status (OK, Warning, Emergency)
    # TODO Color Status (gray, green, yellow, red)
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Set up the Nest Protect sensors from a config entry."""

    data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    entities: list[NestProtectSensor] = []

    SUPPORTED_KEYS = {
        description.key: description for description in SENSOR_DESCRIPTIONS
    }

    for device in data.coordinator.devices.values():
        for key in device.value:
            if description := SUPPORTED_KEYS.get(key):
                entities.append(
                    NestProtectSensor(device.object_key, data.coordinator, description)
                )

    async_add_devices(entities)


class NestProtectSensor(NestDescriptiveEntity, SensorEntity):
    """Representation of a Nest Protect Sensor."""

    entity_description: NestProtectSensorDescription

    @property
    def native_value(self) -> bool:
        """Return the state of the sensor."""
        state = self.bucket.value.get(self.entity_description.key)
        return self.entity_description.value_fn(state)
