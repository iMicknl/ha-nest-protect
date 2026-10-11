"""Tests for the Nest Protect sensor platform."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass

from custom_components.nest_protect.pynest.models import Bucket
from custom_components.nest_protect.sensor import (
    SENSOR_DESCRIPTIONS,
    NestProtectSensor,
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
