"""Tests for the Nest Protect component self-test binary sensors."""

from unittest.mock import MagicMock

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.helpers.entity import EntityCategory
import pytest

from custom_components.nest_protect.binary_sensor import (
    BINARY_SENSOR_DESCRIPTIONS,
    NestProtectBinarySensor,
)
from custom_components.nest_protect.pynest.models import Bucket

DISABLED_BY_DEFAULT_KEYS = ["component_als_test_passed", "component_temp_test_passed"]


def get_description(key: str):
    """Return the binary sensor description for the given key."""
    return next(d for d in BINARY_SENSOR_DESCRIPTIONS if d.key == key)


@pytest.mark.parametrize("key", DISABLED_BY_DEFAULT_KEYS)
def test_component_test_description(key: str):
    """Expose the self-test as a disabled-by-default diagnostic problem sensor."""
    description = get_description(key)

    assert description.translation_key == key
    assert description.device_class == BinarySensorDeviceClass.PROBLEM
    assert description.entity_category == EntityCategory.DIAGNOSTIC
    assert description.entity_registry_enabled_default is False


@pytest.mark.parametrize("key", DISABLED_BY_DEFAULT_KEYS)
@pytest.mark.parametrize(("passed", "problem"), [(True, False), (False, True)])
def test_component_test_reports_problem_when_failed(
    key: str, passed: bool, problem: bool
):
    """A failed self-test is reported as a problem."""
    entity = NestProtectBinarySensor(
        Bucket("topaz.test", 1, 1, {key: passed}),
        get_description(key),
        {},
        MagicMock(),
    )
    assert entity.is_on is problem


@pytest.mark.parametrize(
    "key", ["component_us_test_passed", "component_heat_test_passed"]
)
def test_unreliable_component_tests_have_no_entity(key: str):
    """Healthy Protects report these as failed, so they must not become entities."""
    assert all(d.key != key for d in BINARY_SENSOR_DESCRIPTIONS)
