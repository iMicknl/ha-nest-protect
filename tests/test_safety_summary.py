"""Tests for the home safety summary sensors."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect import (
    HomeAssistantNestProtectData,
    _async_subscribe_for_data,
)
from custom_components.nest_protect.const import DOMAIN
from custom_components.nest_protect.pynest.const import NEST_REQUEST
from custom_components.nest_protect.pynest.enums import BucketType
from custom_components.nest_protect.pynest.models import Bucket, FirstDataAPIResponse
from custom_components.nest_protect.sensor import (
    HOME_SENSOR_DESCRIPTIONS,
    NestHomeSensor,
)

from .conftest import COOKIES, ISSUE_TOKEN


def _bucket(object_key: str, value: dict) -> dict:
    return {
        "object_key": object_key,
        "object_revision": 1,
        "object_timestamp": 1,
        "value": value,
    }


def _first_data(buckets: list[dict]) -> FirstDataAPIResponse:
    return FirstDataAPIResponse(
        weather_for_structures={},
        service_urls={"urls": {"transport_url": "https://t.example.com"}},
        _2fa_enabled=False,
        updated_buckets=buckets,
    )


ACCOUNT_BUCKETS = [
    _bucket("structure.home-1", {"name": "Beach House"}),
    _bucket("structure.home-2", {"name": "Office"}),
    _bucket(
        "topaz.1",
        {"structure_id": "home-1", "serial_number": "ABC123", "smoke_status": 0},
    ),
    _bucket(
        "safety_summary.home-1", {"total_critical_failures": 1, "total_warnings": 2}
    ),
    # The bucket exists, with zeros, for homes without any Protect.
    _bucket(
        "safety_summary.home-2", {"total_critical_failures": 0, "total_warnings": 0}
    ),
]


async def _setup(hass: HomeAssistant, buckets: list[dict]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.nest_protect.NestSessionManager.async_setup",
            return_value=_first_data(buckets),
        ),
        patch(
            "custom_components.nest_protect._async_subscribe_for_data",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.nest_protect._async_observe_locks_loop",
            new_callable=AsyncMock,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return entry


def _entity_id(hass: HomeAssistant, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)


def test_safety_summary_is_requested():
    """The bucket is only delivered (and subscribed to) when requested."""
    assert BucketType.SAFETY_SUMMARY in NEST_REQUEST["known_bucket_types"]


async def test_home_device_created_for_home_with_protect(hass: HomeAssistant):
    """A home with a Protect gets a device named after the structure."""
    entry = await _setup(hass, ACCOUNT_BUCKETS)

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "structure.home-1"), entry.entry_id
    )
    assert device is not None
    assert device.name == "Beach House"
    assert device.manufacturer == "Google"

    for key, expected in (("total_critical_failures", "1"), ("total_warnings", "2")):
        entity_id = _entity_id(hass, f"safety_summary.home-1-{key}")
        assert entity_id is not None
        entity = er.async_get(hass).async_get(entity_id)
        assert entity.device_id == device.id
        assert entity.entity_category is EntityCategory.DIAGNOSTIC
        assert hass.states.get(entity_id).state == expected


async def test_home_without_protect_is_skipped(hass: HomeAssistant):
    """A home without a Protect must not get a device or sensors."""
    entry = await _setup(hass, ACCOUNT_BUCKETS)

    assert (
        dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, "structure.home-2"), entry.entry_id
        )
        is None
    )
    for description in HOME_SENSOR_DESCRIPTIONS:
        assert _entity_id(hass, f"safety_summary.home-2-{description.key}") is None


async def test_home_sensor_entity_names(hass: HomeAssistant):
    """Entity names follow the translated sensor names."""
    await _setup(hass, ACCOUNT_BUCKETS)

    assert hass.states.get("sensor.beach_house_safety_critical_failures") is not None
    assert hass.states.get("sensor.beach_house_safety_warnings") is not None


async def test_missing_values_are_unknown(hass: HomeAssistant):
    """A safety summary without counts reports unknown, not zero."""
    buckets = [
        *ACCOUNT_BUCKETS[:3],
        _bucket("safety_summary.home-1", {}),
    ]
    await _setup(hass, buckets)

    for description in HOME_SENSOR_DESCRIPTIONS:
        entity_id = _entity_id(hass, f"safety_summary.home-1-{description.key}")
        assert hass.states.get(entity_id).state == STATE_UNKNOWN


@pytest.mark.parametrize(
    ("key", "value"),
    [("total_critical_failures", 3), ("total_warnings", 0)],
)
def test_home_sensor_native_value(key: str, value: int):
    """The sensor reports the raw count from the bucket."""
    description = next(d for d in HOME_SENSOR_DESCRIPTIONS if d.key == key)
    bucket = Bucket(**_bucket("safety_summary.home-1", {key: value}))
    sensor = NestHomeSensor(bucket, description, "Beach House", MagicMock())

    assert sensor.native_value == value


def test_home_device_name_falls_back_without_structure_name():
    """A structure without a name still gets a recognisable device."""
    bucket = Bucket(**_bucket("safety_summary.home-1", {}))
    sensor = NestHomeSensor(bucket, HOME_SENSOR_DESCRIPTIONS[0], None, MagicMock())

    assert sensor.device_info["identifiers"] == {(DOMAIN, "structure.home-1")}
    assert sensor.device_info["name"] == "Nest Home"


async def test_subscription_updates_safety_summary(hass: HomeAssistant):
    """Pushed safety summary buckets update the stored bucket and notify entities."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    client = MagicMock()
    sm = MagicMock(ensure_session=AsyncMock(), refreshed_cookies=None)
    initial = Bucket(**_bucket("safety_summary.home-1", {"total_warnings": 0}))
    entry_data = HomeAssistantNestProtectData(
        devices={},
        areas={},
        client=client,
        session_manager=sm,
        grpc_lock_client=MagicMock(),
        safety_summaries={initial.object_key: initial},
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data
    client.subscribe_for_data = AsyncMock(
        return_value={
            "objects": [
                _bucket("safety_summary.home-1", {"total_warnings": 1}),
                _bucket("safety_summary.home-2", {"total_warnings": 5}),
            ]
        }
    )
    data = MagicMock()
    data.service_urls = {"urls": {"transport_url": "https://t.example.com"}}
    data.updated_buckets = []

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch("custom_components.nest_protect.async_dispatcher_send") as dispatch,
    ):
        await _async_subscribe_for_data(hass, entry, data)

    updated = entry_data.safety_summaries["safety_summary.home-1"]
    assert updated.value == {"total_warnings": 1}
    dispatch.assert_called_once_with(hass, "safety_summary.home-1", updated)
    # Homes skipped at setup have no entities to update.
    assert "safety_summary.home-2" not in entry_data.safety_summaries
