"""Sensor platform for Nest Protect."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfElectricPotential,
    UnitOfRatio,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from . import NestProtectConfigEntry
from .entity import NestDescriptiveEntity, NestHomeEntity
from .lock import NestLockBatterySensor, subscribe_to_lock_discovery
from .pynest.enums import BucketType

PARALLEL_UPDATES = 0

SMOKE_CO_STATUS_TO_STATE: dict[int, str] = {
    0: "ok",
    1: "testing",
    2: "warning",
    3: "emergency",
}


def smoke_co_status_to_state(state: int) -> str | None:
    """Convert the raw smoke/CO status code to an enum state."""
    # An unrecognised code must surface as unknown, never as a false "ok".
    return SMOKE_CO_STATUS_TO_STATE.get(state)


def utc_secs_to_datetime(state: int) -> datetime.datetime | None:
    """Convert epoch seconds to an aware UTC datetime."""
    # Nest reports 0 when the event never happened (e.g. no self-test yet).
    if not state:
        return None
    return dt_util.utc_from_timestamp(state)


def utc_secs_to_date(state: int) -> datetime.date | None:
    """Convert epoch seconds to its UTC calendar date."""
    if (value := utc_secs_to_datetime(state)) is None:
        return None
    return value.date()


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

    value_fn: Callable[[Any], StateType | datetime.date] | None = None
    bucket_type: BucketType | None = (
        None  # used to filter out sensors that are not supported by the device
    )
    # Bucket field to read when it differs from `key`; `key` must stay unique
    # because it is also the unique_id suffix.
    value_key: str | None = None

    @property
    def source_key(self) -> str:
        """Return the bucket field this sensor reads."""
        return self.value_key or self.key


SENSOR_DESCRIPTIONS: list[NestProtectSensorDescription] = [
    NestProtectSensorDescription(
        key="battery_level",
        translation_key="battery_level",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=UnitOfRatio.PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        bucket_type=BucketType.KRYPTONITE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    NestProtectSensorDescription(
        key="battery_level",
        translation_key="battery_level",
        value_fn=milli_volt_to_percentage,
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=UnitOfRatio.PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        bucket_type=BucketType.TOPAZ,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    NestProtectSensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        value_key="battery_level",
        value_fn=lambda state: round(state / 1000, 3),
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        bucket_type=BucketType.TOPAZ,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    NestProtectSensorDescription(
        key="replace_by_date_utc_secs",
        translation_key="replace_by_date_utc_secs",
        value_fn=utc_secs_to_date,
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    NestProtectSensorDescription(
        key="last_audio_self_test_end_utc_secs",
        translation_key="last_audio_self_test_end_utc_secs",
        value_fn=utc_secs_to_datetime,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    NestProtectSensorDescription(
        key="latest_manual_test_end_utc_secs",
        translation_key="latest_manual_test_end_utc_secs",
        value_fn=utc_secs_to_datetime,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    NestProtectSensorDescription(
        key="current_temperature",
        translation_key="current_temperature",
        value_fn=lambda state: round(state, 2),
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    NestProtectSensorDescription(
        key="smoke_status",
        translation_key="smoke_status_sensor",
        value_fn=smoke_co_status_to_state,
        device_class=SensorDeviceClass.ENUM,
        options=list(SMOKE_CO_STATUS_TO_STATE.values()),
        icon="mdi:smoke",
    ),
    NestProtectSensorDescription(
        key="co_status",
        translation_key="co_status_sensor",
        value_fn=smoke_co_status_to_state,
        device_class=SensorDeviceClass.ENUM,
        options=list(SMOKE_CO_STATUS_TO_STATE.values()),
        icon="mdi:molecule-co",
    ),
    # A latched peak rather than a live reading, so no state_class.
    NestProtectSensorDescription(
        key="co_previous_peak",
        translation_key="co_previous_peak",
        device_class=SensorDeviceClass.CO,
        native_unit_of_measurement=UnitOfRatio.PARTS_PER_MILLION,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # TODO Add Color Status (gray, green, yellow, red)
]

# MEASUREMENT rather than TOTAL: these count currently open issues and drop
# back when they are resolved, they never accumulate.
HOME_SENSOR_DESCRIPTIONS: list[SensorEntityDescription] = [
    SensorEntityDescription(
        key="total_critical_failures",
        translation_key="total_critical_failures",
        icon="mdi:alert-octagon",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="total_warnings",
        translation_key="total_warnings",
        icon="mdi:alert",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NestProtectConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Set up the Nest Protect sensors from a config entry."""

    data = entry.runtime_data
    entities: list[NestProtectSensor | NestHomeSensor] = []

    for device in data.devices.values():
        for description in SENSOR_DESCRIPTIONS:
            if (
                not description.bucket_type or device.type == description.bucket_type
            ) and description.source_key in device.value:
                entities.append(
                    NestProtectSensor(device, description, data.areas, data.client)
                )

    for safety_summary in data.safety_summaries.values():
        home_name = data.structure_names.get(
            safety_summary.object_key.removeprefix("safety_summary.")
        )
        entities.extend(
            NestHomeSensor(safety_summary, description, home_name, data.client)
            for description in HOME_SENSOR_DESCRIPTIONS
        )

    async_add_devices(entities)

    # Battery sensors for any discovered Nest x Yale locks.
    subscribe_to_lock_discovery(hass, entry, async_add_devices, NestLockBatterySensor)


class NestProtectSensor(NestDescriptiveEntity, SensorEntity):
    """Representation of a Nest Protect Sensor."""

    entity_description: NestProtectSensorDescription

    @property
    def native_value(self) -> StateType | datetime.date:
        """Return the state of the sensor."""
        state = self.bucket.value.get(self.entity_description.source_key)
        if state is None:
            return None

        if self.entity_description.value_fn:
            return self.entity_description.value_fn(state)

        return state


class NestHomeSensor(NestHomeEntity, SensorEntity):
    """Representation of a Nest home sensor."""

    @property
    def native_value(self) -> StateType:
        """Return the state of the sensor."""
        return self.bucket.value.get(self.entity_description.key)
