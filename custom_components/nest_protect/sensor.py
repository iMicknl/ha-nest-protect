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
from homeassistant.const import PERCENTAGE, TEMP_CELSIUS
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.typing import StateType

from . import HomeAssistantNestProtectData
from .const import DOMAIN
from .entity import NestDescriptiveEntity


@dataclass
class NestProtectSensorDescription(SensorEntityDescription):
    """Class to describe an Nest Protect sensor."""

    value_fn: Callable[[Any], StateType] | None = None


SENSOR_DESCRIPTIONS: list[SensorEntityDescription] = [
    NestProtectSensorDescription(
        key="battery_level",
        name="Battery Level",
        value_fn=lambda state: state if state <= 100 else None,
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    NestProtectSensorDescription(
        name="Replace By",
        key="replace_by_date_utc_secs",
        value_fn=lambda state: datetime.datetime.utcfromtimestamp(state),
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    NestProtectSensorDescription(
        name="Temperature",
        key="current_temperature",
        value_fn=lambda state: round(state, 2),
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=TEMP_CELSIUS,
    ),
    # TODO Add Color Status (gray, green, yellow, red)
    # TODO Smoke Status (OK, Warning, Emergency)
    # TODO CO Status (OK, Warning, Emergency)
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Set up the Nest Protect sensors from a config entry."""

    data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    entities: list[NestProtectSensor] = []

    SUPPORTED_KEYS = {
        description.key: description for description in SENSOR_DESCRIPTIONS
    }

    for device in data.devices.values():
        for key in device.value:
            if description := SUPPORTED_KEYS.get(key):
                entities.append(
                    NestProtectSensor(device, description, data.areas, data.client)
                )

    async_add_devices(entities)


class NestProtectSensor(NestDescriptiveEntity, SensorEntity):
    """Representation of a Nest Protect Sensor."""

    entity_description: NestProtectSensorDescription

    @property
    def native_value(self) -> bool:
        """Return the state of the sensor."""
        state = self.bucket.value.get(self.entity_description.key)

        if self.entity_description.value_fn:
            return self.entity_description.value_fn(state)

        return state
