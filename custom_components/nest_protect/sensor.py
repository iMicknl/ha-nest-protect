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
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.typing import StateType

from . import HomeAssistantNestProtectData
from .const import DOMAIN
from .entity import NestDescriptiveEntity
from .pynest.enums import BucketType


def milli_volt_to_percentage(state: int):
    """
    Convert battery level in mV to a percentage.

    The battery life percentage in devices is estimated using slopes from the L91 battery's datasheet.
    This is a rough estimation, and the battery life percentage is not linear.

    Tests on various devices have shown accurate results.
    """
    if 3000 < state <= 6000:
        if 4950 < state <= 6000:
            slope = 0.001816609
            yint = -8.548096886
        elif 4800 < state <= 4950:
            slope = 0.000291667
            yint = -0.991176471
        elif 4500 < state <= 4800:
            slope = 0.001077342
            yint = -4.730392157
        else:
            slope = 0.000434641
            yint = -1.825490196

        return max(0, min(100, round(((slope * state) + yint) * 100)))

    return None


@dataclass
class NestProtectSensorDescription(SensorEntityDescription):
    """Class to describe an Nest Protect sensor."""

    value_fn: Callable[[Any], StateType] | None = None
    bucket_type: BucketType | None = (
        None  # used to filter out sensors that are not supported by the device
    )


SENSOR_DESCRIPTIONS: list[NestProtectSensorDescription] = [
    NestProtectSensorDescription(
        key="battery_level",
        name="Battery Level",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        bucket_type=BucketType.KRYPTONITE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # TODO Due to duplicate keys, this sensor is not available yet
    # NestProtectSensorDescription(
    #     key="battery_level",
    #     name="Battery Voltage",
    #     value_fn=lambda state: round(state / 1000, 3),
    #     device_class=SensorDeviceClass.BATTERY,
    #     native_unit_of_measurement=UnitOfElectricPotential.VOLT,
    #     entity_category=EntityCategory.DIAGNOSTIC,
    #     bucket_type=BucketType.TOPAZ,
    # ),
    NestProtectSensorDescription(
        key="battery_level",
        name="Battery Level",
        value_fn=lambda state: milli_volt_to_percentage(state),
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        bucket_type=BucketType.TOPAZ,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    NestProtectSensorDescription(
        name="Replace By",
        key="replace_by_date_utc_secs",
        value_fn=lambda state: datetime.datetime.utcfromtimestamp(state),
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    NestProtectSensorDescription(
        name="Last Audio Self Test",
        key="last_audio_self_test_end_utc_secs",
        value_fn=lambda state: datetime.datetime.utcfromtimestamp(state),
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    NestProtectSensorDescription(
        name="Last Manual Test",
        key="latest_manual_test_end_utc_secs",
        value_fn=lambda state: datetime.datetime.utcfromtimestamp(state),
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    NestProtectSensorDescription(
        name="Temperature",
        key="current_temperature",
        value_fn=lambda state: round(state, 2),
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # TODO Add Color Status (gray, green, yellow, red)
    # TODO Smoke Status (OK, Warning, Emergency)
    # TODO CO Status (OK, Warning, Emergency)
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Set up the Nest Protect sensors from a config entry."""

    data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    entities: list[NestProtectSensor] = []

    for device in data.devices.values():

        SUPPORTED_KEYS: dict[str, NestProtectSensorDescription] = {
            description.key: description
            for description in SENSOR_DESCRIPTIONS
            if (not description.bucket_type or device.type == description.bucket_type)
        }

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
