"""Tests for sparse Nest Protect device responses."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.nest_protect.binary_sensor import (
    BINARY_SENSOR_DESCRIPTIONS,
    NestProtectBinarySensor,
    async_setup_entry,
)
from custom_components.nest_protect.const import DOMAIN
from custom_components.nest_protect.pynest.enums import BucketType
from custom_components.nest_protect.pynest.models import Bucket
from custom_components.nest_protect.sensor import (
    SENSOR_DESCRIPTIONS,
    NestProtectSensor,
)


def make_bucket(value):
    """Make a device response without optional identity metadata."""
    return Bucket("topaz.test", 1, 1, value)


@pytest.mark.parametrize("key", ["smoke_status", "co_status", "auto_away"])
@pytest.mark.parametrize("missing", [False, True])
def test_missing_binary_values_remain_unknown(key, missing):
    """Missing samples must not be converted into an alarm or occupancy."""
    description = next(d for d in BINARY_SENSOR_DESCRIPTIONS if d.key == key)
    payload = {} if missing else {key: None}
    entity = NestProtectBinarySensor(make_bucket(payload), description, {}, MagicMock())
    assert entity.is_on is None


@pytest.mark.parametrize(
    "key", ["battery_level", "current_temperature", "replace_by_date_utc_secs"]
)
@pytest.mark.parametrize("missing", [False, True])
def test_missing_numeric_values_remain_unknown(key, missing):
    """Missing numeric/date samples must not raise during state writes."""
    description = next(
        d
        for d in SENSOR_DESCRIPTIONS
        if d.key == key and d.bucket_type != BucketType.KRYPTONITE
    )
    entity = NestProtectSensor(
        make_bucket({} if missing else {key: None}), description, {}, MagicMock()
    )
    assert entity.native_value is None


@pytest.mark.parametrize("missing", [False, True])
async def test_unknown_power_source_does_not_abort_binary_platform(hass, missing):
    """A sparse Protect must still expose its supported alarm sensors."""
    payload = {
        "smoke_status": 0,
        "auto_away": False,
        "line_power_present": True,
    }
    if not missing:
        payload["wired_or_battery"] = None
    device = make_bucket(payload)
    entry = SimpleNamespace(entry_id="test")
    hass.data[DOMAIN] = {
        entry.entry_id: SimpleNamespace(
            devices={device.object_key: device}, areas={}, client=MagicMock()
        )
    }
    add_entities = MagicMock()

    await async_setup_entry(hass, entry, add_entities)

    entities = add_entities.call_args.args[0]
    assert [entity.entity_description.key for entity in entities] == ["smoke_status"]
    assert entities[0].is_on is False


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("smoke_status", 0, False),
        ("smoke_status", 2, True),
        ("co_status", 3, True),
        ("auto_away", False, True),
    ],
)
def test_present_binary_values_keep_existing_meaning(key, value, expected):
    """Preserve the meaning of real alarm and occupancy samples."""
    description = next(d for d in BINARY_SENSOR_DESCRIPTIONS if d.key == key)
    entity = NestProtectBinarySensor(
        make_bucket({key: value}), description, {}, MagicMock()
    )
    assert entity.is_on is expected
