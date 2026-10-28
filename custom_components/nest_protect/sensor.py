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
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from . import NestProtectConfigEntry
from .entity import NestDescriptiveEntity, NestHomeEntity, resolve_area
from .lock import NestLockBatterySensor, subscribe_to_lock_discovery
from .pynest.client import NestClient
from .pynest.enums import BucketType
from .pynest.models import Bucket
from .pynest.protobuf import (
    RCS_SOURCE_TYPE_BACKPLATE,
    RCS_SOURCE_TYPE_MULTI_SENSOR,
    RCS_SOURCE_TYPE_SINGLE_SENSOR,
)
from .thermostat import subscribe_to_thermostat_discovery

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
    NestProtectSensorDescription(
        key="backplate_temperature",
        translation_key="backplate_temperature",
        value_fn=lambda state: round(state, 2),
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        bucket_type=BucketType.DEVICE,
    ),
    NestProtectSensorDescription(
        key="current_humidity",
        translation_key="current_humidity",
        value_fn=round,
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=UnitOfRatio.PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        bucket_type=BucketType.DEVICE,
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

# The thermostat's own sensor, the effective reading it controls on — which
# follows the selected remote comfort sensor and is what the SDM API reports —
# and its humidity.
THERMOSTAT_SENSOR_KEYS = (
    "backplate_temperature",
    "current_temperature",
    "current_humidity",
)

ACTIVE_TEMPERATURE_SENSOR_DESCRIPTION = NestProtectSensorDescription(
    key="active_temperature_sensor",
    translation_key="active_temperature_sensor",
    bucket_type=BucketType.DEVICE,
)

# State shown when the thermostat controls on its own backplate sensor rather
# than on a Nest Temperature Sensor.
THERMOSTAT_SOURCE_LABEL = "Thermostat"

RCS_SOURCE_TYPE_NAMES: dict[int | None, str] = {
    RCS_SOURCE_TYPE_BACKPLATE: "backplate",
    RCS_SOURCE_TYPE_SINGLE_SENSOR: "single_sensor",
    RCS_SOURCE_TYPE_MULTI_SENSOR: "multi_sensor",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NestProtectConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Set up the Nest Protect sensors from a config entry."""

    data = entry.runtime_data
    entities: list[NestProtectSensor | NestHomeSensor] = []

    for device in data.devices.values():
        # Thermostats come in through protobuf discovery below.
        if device.type == BucketType.DEVICE:
            continue

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

    # Temperature sensors for any thermostat seen on the protobuf stream.
    thermostat_descriptions = [
        description
        for description in SENSOR_DESCRIPTIONS
        if description.key in THERMOSTAT_SENSOR_KEYS
    ]
    subscribe_to_thermostat_discovery(
        hass,
        entry,
        async_add_devices,
        lambda bucket: (
            [
                NestThermostatSensor(bucket, description, data.areas, data.client)
                for description in thermostat_descriptions
            ]
            + [
                NestThermostatActiveSensor(
                    bucket,
                    ACTIVE_TEMPERATURE_SENSOR_DESCRIPTION,
                    data.areas,
                    data.client,
                    data.devices,
                )
            ]
        ),
    )


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


class NestThermostatSensor(NestProtectSensor):
    """Temperature sensor on a thermostat discovered via the protobuf stream."""

    def __init__(
        self,
        bucket: Bucket,
        description: NestProtectSensorDescription,
        areas: dict[str, str],
        client: NestClient,
    ) -> None:
        """Initialize, keeping the area map to resolve late `where_id` traits."""
        super().__init__(bucket, description, areas, client)
        self._areas = areas
        self._identity = self._identity_snapshot()

    def _identity_snapshot(self) -> tuple:
        """Snapshot the bucket values that feed DeviceInfo.

        The observe loop mutates the bucket in place, so the previous values have
        to be remembered here rather than read back off the incoming bucket.
        """
        return tuple(
            self.bucket.value.get(key)
            for key in ("model", "current_version", "where_id", "where_label")
        )

    @callback
    def update_callback(self, bucket: Bucket) -> None:
        """Update the entity, refreshing device details that arrive late.

        `DeviceInfo` is only read when the entity is added, but the model,
        firmware version and room label arrive in traits published after the one
        that first reveals the thermostat. Those have to be written to the
        registry directly, the same way `lock.py` does it.
        """
        self.area = resolve_area(bucket.value, self._areas)
        super().update_callback(bucket)

        if (identity := self._identity_snapshot()) == self._identity:
            return
        self._identity = identity

        if not (device_entry := self.device_entry):
            return

        if (device_info := self.generate_device_info()) is None:
            return

        dr.async_get(self.hass).async_update_device(
            device_entry.id,
            name=device_info.get("name"),
            model=device_info.get("model"),
            sw_version=device_info.get("sw_version"),
        )


class NestThermostatActiveSensor(NestThermostatSensor):
    """Which temperature source the thermostat is currently controlling on.

    Read-only. Google's SDM API offers no equivalent: it reports the effective
    temperature without saying where it comes from.
    """

    def __init__(
        self,
        bucket: Bucket,
        description: NestProtectSensorDescription,
        areas: dict[str, str],
        client: NestClient,
        devices: dict[str, Bucket],
    ) -> None:
        """Initialize with the device map, to name the selected sensor."""
        super().__init__(bucket, description, areas, client)
        self._devices = devices
        self._sensor_unsubs: dict[str, CALLBACK_TYPE] = {}

    async def async_added_to_hass(self) -> None:
        """Subscribe to this thermostat, and to the sensors it names."""
        await super().async_added_to_hass()
        self.async_on_remove(self._unsubscribe_sensors)
        self._resubscribe_sensors()

    @callback
    def update_callback(self, bucket: Bucket) -> None:
        """Handle a thermostat update, following any change of selection."""
        super().update_callback(bucket)
        self._resubscribe_sensors()

    @callback
    def _resubscribe_sensors(self) -> None:
        """Track bucket updates for the sensors this entity names.

        A temperature sensor's room label lands in its own `where_id` trait,
        routinely after the selection that points at it. Without following those
        buckets the state would keep showing the bare device id until the next
        thermostat update happened to arrive.
        """
        wanted = set(self.bucket.value.get("active_rcs_sensors") or [])

        for object_key in wanted - self._sensor_unsubs.keys():
            self._sensor_unsubs[object_key] = async_dispatcher_connect(
                self.hass, object_key, self._sensor_updated
            )

        for object_key in self._sensor_unsubs.keys() - wanted:
            self._sensor_unsubs.pop(object_key)()

    @callback
    def _unsubscribe_sensors(self) -> None:
        """Drop every sensor-bucket subscription."""
        while self._sensor_unsubs:
            _, unsub = self._sensor_unsubs.popitem()
            unsub()

    @callback
    def _sensor_updated(self, bucket: Bucket) -> None:
        """Re-resolve the name after a selected sensor publishes new traits."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        """Return a human-readable name for the active temperature source."""
        source_type = self.bucket.value.get("rcs_source_type")
        active = self.bucket.value.get("active_rcs_sensors") or []

        if source_type == RCS_SOURCE_TYPE_BACKPLATE:
            return THERMOSTAT_SOURCE_LABEL

        if source_type == RCS_SOURCE_TYPE_SINGLE_SENSOR and active:
            return self._sensor_label(active[0])

        if source_type == RCS_SOURCE_TYPE_MULTI_SENSOR and active:
            return ", ".join(sorted(self._sensor_label(key) for key in active))

        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw selection, for automations and troubleshooting."""
        source_type = self.bucket.value.get("rcs_source_type")

        return {
            "source_type": RCS_SOURCE_TYPE_NAMES.get(source_type, "unknown"),
            "active_sensors": self.bucket.value.get("active_rcs_sensors") or [],
            "associated_sensors": self.bucket.value.get("associated_rcs_sensors") or [],
        }

    def _sensor_label(self, object_key: str) -> str:
        """Name a temperature sensor by its own label, else its room."""
        sensor = self._devices.get(object_key)
        label = sensor and (
            sensor.value.get("description") or resolve_area(sensor.value, self._areas)
        )

        return label or object_key.removeprefix("kryptonite.")


class NestHomeSensor(NestHomeEntity, SensorEntity):
    """Representation of a Nest home sensor."""

    @property
    def native_value(self) -> StateType:
        """Return the state of the sensor."""
        return self.bucket.value.get(self.entity_description.key)
