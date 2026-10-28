"""Tests for the Nest Protect sensor platform."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    CONCENTRATION_PARTS_PER_MILLION,
    EntityCategory,
    UnitOfElectricPotential,
)

from custom_components.nest_protect.pynest.models import Bucket
from custom_components.nest_protect.sensor import (
    SENSOR_DESCRIPTIONS,
    NestProtectSensor,
    async_setup_entry,
    smoke_co_status_to_state,
)

AREAS = {"where.living-room": "Living Room"}


def build_sensor(
    key: str, value: dict, object_key: str = "topaz.1234"
) -> NestProtectSensor:
    """Build a NestProtectSensor for the given entity description key."""
    bucket = Bucket(
        object_key=object_key,
        object_revision=1,
        object_timestamp=1,
        value=value,
    )
    description = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
    return NestProtectSensor(
        bucket=bucket,
        description=description,
        areas=AREAS,
        client=MagicMock(),
    )


@pytest.mark.parametrize(
    ("code", "expected"),
    [(0, "ok"), (1, "testing"), (2, "warning"), (3, "emergency"), (99, None)],
)
def test_smoke_co_status_to_state(code: int, expected: str | None):
    """Test that raw status codes map to the enum states and unknown codes to None."""
    assert smoke_co_status_to_state(code) == expected


@pytest.mark.parametrize(
    ("key", "code", "expected"),
    [
        ("smoke_status", 0, "ok"),
        ("smoke_status", 1, "testing"),
        ("smoke_status", 2, "warning"),
        ("smoke_status", 3, "emergency"),
        ("co_status", 0, "ok"),
        ("co_status", 2, "warning"),
        ("co_status", 3, "emergency"),
    ],
)
def test_smoke_and_co_status_sensor_native_value(key: str, code: int, expected: str):
    """Test that the smoke_status/co_status enum sensors surface the mapped state."""
    sensor = build_sensor(key, {key: code})
    assert sensor.native_value == expected


@pytest.mark.parametrize("key", ["smoke_status", "co_status"])
def test_smoke_and_co_status_sensor_unknown_when_missing(key: str):
    """Test that a missing or unrecognised status reports unknown rather than ok."""
    assert build_sensor(key, {}).native_value is None
    assert build_sensor(key, {key: 99}).native_value is None


def test_co_previous_peak_description():
    """Test that the CO previous peak is a diagnostic CO sensor without a state class."""
    description = next(d for d in SENSOR_DESCRIPTIONS if d.key == "co_previous_peak")
    assert description.translation_key == "co_previous_peak"
    assert description.device_class == SensorDeviceClass.CO
    assert description.native_unit_of_measurement == CONCENTRATION_PARTS_PER_MILLION
    assert description.entity_category == EntityCategory.DIAGNOSTIC
    assert description.state_class is None


def test_co_previous_peak_native_value():
    """Test that the CO previous peak reports the raw ppm value."""
    assert build_sensor("co_previous_peak", {"co_previous_peak": 42}).native_value == 42
    assert build_sensor("co_previous_peak", {"co_previous_peak": 0}).native_value == 0


def test_co_previous_peak_unknown_when_none():
    """Test that a None CO previous peak reports unknown."""
    sensor = build_sensor("co_previous_peak", {"co_previous_peak": None})
    assert sensor.native_value is None


@pytest.mark.parametrize(
    ("value", "expected_keys"),
    [
        ({"co_status": 0, "co_previous_peak": 7}, ["co_status", "co_previous_peak"]),
        ({"co_status": 0}, ["co_status"]),
    ],
)
async def test_co_previous_peak_only_created_when_reported(
    hass, value: dict, expected_keys: list[str]
):
    """Test that the CO previous peak sensor is only created when the device reports it."""
    device = Bucket(
        object_key="topaz.1234", object_revision=1, object_timestamp=1, value=value
    )
    entry = MagicMock(entry_id="test")
    entry.runtime_data = SimpleNamespace(
        devices={device.object_key: device},
        areas=AREAS,
        client=MagicMock(),
        lock_state_cache={},
        safety_summaries={},
        structure_names={},
    )
    add_entities = MagicMock()

    await async_setup_entry(hass, entry, add_entities)

    entities = add_entities.call_args.args[0]
    assert [entity.entity_description.key for entity in entities] == expected_keys


@pytest.mark.parametrize(("millivolts", "volts"), [(5400, 5.4), (2987, 2.987)])
def test_battery_voltage_sensor_native_value(millivolts: int, volts: float):
    """Test that the battery voltage sensor converts millivolts to volts."""
    sensor = build_sensor("battery_voltage", {"battery_level": millivolts})
    assert sensor.native_value == volts


def test_battery_voltage_sensor_unknown_when_missing():
    """Test that a missing battery level reports unknown."""
    assert build_sensor("battery_voltage", {}).native_value is None
    assert build_sensor("battery_voltage", {"battery_level": None}).native_value is None


def test_battery_voltage_sensor_description():
    """Test the battery voltage sensor is a disabled-by-default diagnostic."""
    description = next(d for d in SENSOR_DESCRIPTIONS if d.key == "battery_voltage")
    assert description.device_class is SensorDeviceClass.VOLTAGE
    assert description.native_unit_of_measurement == UnitOfElectricPotential.VOLT
    assert description.state_class is SensorStateClass.MEASUREMENT
    assert description.entity_category is EntityCategory.DIAGNOSTIC
    assert description.entity_registry_enabled_default is False
    assert description.suggested_display_precision == 3


def test_battery_sensors_have_distinct_unique_ids():
    """Test that both sensors on battery_level keep separate unique ids."""
    value = {"battery_level": 5400}
    assert build_sensor("battery_level", value).unique_id == "topaz.1234-battery_level"
    assert (
        build_sensor("battery_voltage", value).unique_id == "topaz.1234-battery_voltage"
    )


@pytest.mark.parametrize(
    ("object_key", "expected_keys"),
    [
        ("topaz.1234", ["battery_level", "battery_voltage"]),
        ("kryptonite.1234", ["battery_level"]),
    ],
)
async def test_battery_voltage_sensor_only_created_for_protect(
    hass, object_key: str, expected_keys: list[str]
):
    """Test that only Protect devices get a battery voltage sensor."""
    device = Bucket(object_key, 1, 1, {"battery_level": 5400})
    entry = SimpleNamespace(entry_id="test")
    entry.runtime_data = SimpleNamespace(
        devices={device.object_key: device},
        areas={},
        client=MagicMock(),
        safety_summaries={},
        structure_names={},
    )
    add_entities = MagicMock()

    with patch("custom_components.nest_protect.sensor.subscribe_to_lock_discovery"):
        await async_setup_entry(hass, entry, add_entities)

    entities = add_entities.call_args.args[0]
    assert [entity.entity_description.key for entity in entities] == expected_keys


@pytest.mark.parametrize(
    "key", ["last_audio_self_test_end_utc_secs", "latest_manual_test_end_utc_secs"]
)
def test_self_test_sensor_is_timezone_aware_timestamp(key: str):
    """Test that self-test sensors keep the time of day as an aware UTC timestamp."""
    sensor = build_sensor(key, {key: 1700000000})
    assert sensor.device_class == SensorDeviceClass.TIMESTAMP
    assert sensor.native_value == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def test_replace_by_sensor_is_utc_calendar_date():
    """Test that the replace-by sensor reports the UTC calendar date."""
    key = "replace_by_date_utc_secs"
    # 23:30 UTC, so a local-time conversion east of UTC would roll to the next day.
    sensor = build_sensor(key, {key: 1893454200})
    assert sensor.device_class == SensorDeviceClass.DATE
    assert type(sensor.native_value) is date
    assert sensor.native_value == date(2029, 12, 31)


@pytest.mark.parametrize(
    "key",
    [
        "replace_by_date_utc_secs",
        "last_audio_self_test_end_utc_secs",
        "latest_manual_test_end_utc_secs",
    ],
)
@pytest.mark.parametrize("value", [{}, {"raw": None}, {"raw": 0}], ids=str)
def test_date_sensors_unknown_when_missing_or_zero(key: str, value: dict):
    """Test that a missing or zero (never run) value reports unknown, not 1970."""
    sensor = build_sensor(key, {key: value["raw"]} if value else {})
    assert sensor.native_value is None
