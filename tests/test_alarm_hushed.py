"""Tests for the Nest Protect alarm hushed binary sensor."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.nest_protect.binary_sensor import (
    BINARY_SENSOR_DESCRIPTIONS,
    NestProtectBinarySensor,
    async_setup_entry,
)
from custom_components.nest_protect.pynest.models import Bucket


def make_bucket(value: dict) -> Bucket:
    """Make a Nest Protect device bucket with the given values."""
    return Bucket("topaz.test", 1, 1, value)


async def setup_entities(hass, value: dict) -> list[NestProtectBinarySensor]:
    """Run the binary sensor platform setup for a single device."""
    device = make_bucket(value)
    entry = SimpleNamespace(entry_id="test")
    entry.runtime_data = SimpleNamespace(
        devices={device.object_key: device},
        areas={},
        client=MagicMock(),
        widget_tracks={},
    )
    add_entities = MagicMock()

    await async_setup_entry(hass, entry, add_entities)

    return add_entities.call_args.args[0]


async def test_alarm_hushed_created_when_reported(hass):
    """Devices reporting a hush state get an alarm hushed entity."""
    entities = await setup_entities(hass, {"hushed_state": False})

    [entity] = [e for e in entities if e.entity_description.key == "hushed_state"]
    assert entity.entity_description.translation_key == "hushed_state"
    assert entity.entity_description.device_class is None
    assert entity.entity_description.icon == "mdi:bell-sleep"
    assert entity.is_on is False


async def test_alarm_hushed_not_created_when_absent(hass):
    """Devices that never report a hush state get no alarm hushed entity."""
    entities = await setup_entities(hass, {"smoke_status": 0})

    assert "hushed_state" not in [e.entity_description.key for e in entities]


@pytest.mark.parametrize(
    ("value", "expected"), [(True, True), (False, False), (None, None)]
)
def test_alarm_hushed_state(value, expected):
    """The entity is on while hushed and unknown when the value is missing."""
    description = next(d for d in BINARY_SENSOR_DESCRIPTIONS if d.key == "hushed_state")
    entity = NestProtectBinarySensor(
        make_bucket({"hushed_state": value}), description, {}, MagicMock()
    )
    assert entity.is_on is expected
