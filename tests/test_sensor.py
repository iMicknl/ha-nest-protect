"""Tests for the Nest Protect sensor platform."""

from unittest.mock import MagicMock

import pytest

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
