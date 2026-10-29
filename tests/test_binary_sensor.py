"""Tests for the Nest Protect binary sensor platform."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.nest_protect import (
    HomeAssistantNestProtectData,
    _async_subscribe_for_data,
)
from custom_components.nest_protect.binary_sensor import (
    BINARY_SENSOR_DESCRIPTIONS,
    CONNECTIVITY_DESCRIPTION,
    NestProtectConnectivitySensor,
    NestProtectOccupancySensor,
    async_setup_entry,
)
from custom_components.nest_protect.const import DOMAIN
from custom_components.nest_protect.pynest.const import NEST_REQUEST
from custom_components.nest_protect.pynest.enums import BucketType
from custom_components.nest_protect.pynest.models import Bucket, FirstDataAPIResponse
from custom_components.nest_protect.session import NestSessionManager

from .conftest import ComponentSetup

TOPAZ_KEY = "topaz.1234"
WIDGET_TRACK_KEY = "widget_track.1234"
CONNECTIVITY_UNIQUE_ID = f"{TOPAZ_KEY}-is_online"


def make_bucket(object_key: str, value: dict) -> Bucket:
    """Make a bucket as returned by the Nest API."""
    return Bucket(object_key, 1, 1, value)


def build_connectivity_sensor(
    widget_track: Bucket | None,
) -> NestProtectConnectivitySensor:
    """Build a connectivity sensor for a Protect."""
    return NestProtectConnectivitySensor(
        make_bucket(TOPAZ_KEY, {"smoke_status": 0}),
        CONNECTIVITY_DESCRIPTION,
        {},
        MagicMock(),
        widget_track_key=WIDGET_TRACK_KEY,
        widget_track=widget_track,
    )


def test_widget_track_buckets_are_requested():
    """Online state is only published in widget_track buckets."""
    assert BucketType.WIDGET_TRACK in NEST_REQUEST["known_bucket_types"]


@pytest.mark.parametrize(
    ("widget_track", "expected"),
    [
        (make_bucket(WIDGET_TRACK_KEY, {"online": True}), True),
        (make_bucket(WIDGET_TRACK_KEY, {"online": False}), False),
        (make_bucket(WIDGET_TRACK_KEY, {"online": None}), None),
        (make_bucket(WIDGET_TRACK_KEY, {}), None),
        (None, None),
    ],
)
def test_connectivity_state(widget_track: Bucket | None, expected: bool | None):
    """Missing connectivity data is unknown, never offline."""
    assert build_connectivity_sensor(widget_track).is_on is expected


def test_connectivity_sensor_ignores_entity_availability():
    """An offline Protect must report off rather than become unavailable."""
    sensor = build_connectivity_sensor(make_bucket(WIDGET_TRACK_KEY, {"online": False}))
    sensor._attr_available = False

    assert sensor.available is True


async def test_setup_creates_connectivity_sensor_per_protect(hass):
    """Every Protect gets a connectivity sensor, with or without widget_track data."""
    protect = make_bucket(TOPAZ_KEY, {"smoke_status": 0})
    protect_without_track = make_bucket("topaz.5678", {"smoke_status": 0})
    sensor = make_bucket("kryptonite.9999", {"current_temperature": 20})
    widget_track = make_bucket(WIDGET_TRACK_KEY, {"online": True})
    entry = SimpleNamespace(entry_id="test")
    entry.runtime_data = SimpleNamespace(
        devices={
            bucket.object_key: bucket
            for bucket in (protect, protect_without_track, sensor)
        },
        widget_tracks={WIDGET_TRACK_KEY: widget_track},
        areas={},
        client=MagicMock(),
    )
    add_entities = MagicMock()

    await async_setup_entry(hass, entry, add_entities)

    connectivity = {
        entity.unique_id: entity
        for entity in add_entities.call_args.args[0]
        if isinstance(entity, NestProtectConnectivitySensor)
    }
    assert set(connectivity) == {CONNECTIVITY_UNIQUE_ID, "topaz.5678-is_online"}
    assert connectivity[CONNECTIVITY_UNIQUE_ID].is_on is True
    assert connectivity["topaz.5678-is_online"].is_on is None


async def test_connectivity_sensor_follows_widget_track_updates(hass):
    """Dispatched widget_track buckets update the sensor state."""
    sensor = build_connectivity_sensor(None)
    sensor.hass = hass
    sensor.entity_id = "binary_sensor.nest_protect_online"
    # Built without a platform, so there is no entry to read stream health from.
    sensor._follows_stream_health = False

    with patch.object(sensor, "async_write_ha_state") as write_state:
        await sensor.async_added_to_hass()
        async_dispatcher_send(
            hass, WIDGET_TRACK_KEY, make_bucket(WIDGET_TRACK_KEY, {"online": False})
        )

    write_state.assert_called_once()
    assert sensor.is_on is False


async def test_connectivity_from_first_data_and_subscriber(
    hass, component_setup_with_cookies: ComponentSetup
):
    """Widget tracks from the first fetch and the subscriber drive the sensor."""
    first_data = FirstDataAPIResponse(
        weather_for_structures={},
        service_urls={"urls": {"transport_url": "https://t.example.com"}},
        _2fa_enabled=False,
        updated_buckets=[
            {
                "object_key": TOPAZ_KEY,
                "object_revision": 1,
                "object_timestamp": 1,
                "value": {"smoke_status": 0},
            },
            {
                "object_key": WIDGET_TRACK_KEY,
                "object_revision": 1,
                "object_timestamp": 1,
                "value": {"online": True},
            },
        ],
    )

    with (
        patch.object(NestSessionManager, "async_setup", return_value=first_data),
        patch("custom_components.nest_protect._async_subscribe_for_data"),
        patch("custom_components.nest_protect._async_observe_locks_loop"),
    ):
        await component_setup_with_cookies()

    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, CONNECTIVITY_UNIQUE_ID
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == STATE_ON

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    entry_data: HomeAssistantNestProtectData = entry.runtime_data
    entry_data.client.nest_session = MagicMock()
    entry_data.client.subscribe_for_data = AsyncMock(
        return_value={
            "objects": [
                {
                    "object_key": WIDGET_TRACK_KEY,
                    "object_revision": 2,
                    "object_timestamp": 2,
                    "value": {"online": False},
                }
            ]
        }
    )

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(NestSessionManager, "ensure_session", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, first_data)
        await hass.async_block_till_done()

    assert entry_data.widget_tracks[WIDGET_TRACK_KEY].value == {"online": False}
    assert hass.states.get(entity_id).state == STATE_OFF

    entry_data.client.subscribe_for_data.return_value = {
        "objects": [
            {
                "object_key": WIDGET_TRACK_KEY,
                "object_revision": 3,
                "object_timestamp": 3,
                "value": {},
            }
        ]
    }

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(NestSessionManager, "ensure_session", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, first_data)
        await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == STATE_UNKNOWN


OCCUPANCY_UNIQUE_ID = f"{TOPAZ_KEY}-auto_away"


def build_occupancy_sensor(value: dict) -> NestProtectOccupancySensor:
    """Build an occupancy sensor for a wired Protect."""
    description = next(d for d in BINARY_SENSOR_DESCRIPTIONS if d.key == "auto_away")
    sensor = NestProtectOccupancySensor(
        make_bucket(TOPAZ_KEY, {"wired_or_battery": 0, **value}),
        description,
        {},
        MagicMock(),
    )
    # Built without a platform, so there is no entry to read stream health from.
    sensor._follows_stream_health = False
    return sensor


@pytest.mark.parametrize(
    ("software_version", "available"),
    [
        ("3.1.4rc3", True),
        ("3.6rc8", True),
        ("5.0rc31", False),
        ("5.0rc38", False),
        ("6.0", False),
        (None, True),
        ("", True),
        ("unknown", True),
    ],
)
def test_occupancy_unavailable_on_firmware_without_motion_reports(
    software_version: str | None, available: bool
):
    """Firmware 5 and later freezes auto_away, so its value is not shown."""
    sensor = build_occupancy_sensor(
        {"auto_away": False, "software_version": software_version}
    )

    assert sensor.available is available


def test_occupancy_available_once_auto_away_changes():
    """A change proves the Protect still reports motion despite its firmware."""
    sensor = build_occupancy_sensor({"auto_away": False, "software_version": "5.0rc38"})

    with patch.object(sensor, "async_write_ha_state"):
        sensor.update_callback(
            make_bucket(
                TOPAZ_KEY,
                {
                    "wired_or_battery": 0,
                    "auto_away": False,
                    "software_version": "5.0rc38",
                },
            )
        )
        assert sensor.available is False

        sensor.update_callback(
            make_bucket(
                TOPAZ_KEY,
                {
                    "wired_or_battery": 0,
                    "auto_away": True,
                    "software_version": "5.0rc38",
                },
            )
        )

    assert sensor.available is True
    assert sensor.is_on is False


def test_occupancy_missing_sample_is_not_a_change():
    """A sparse update must not count as the Protect reporting motion."""
    sensor = build_occupancy_sensor({"auto_away": False, "software_version": "5.0rc38"})

    with patch.object(sensor, "async_write_ha_state"):
        sensor.update_callback(make_bucket(TOPAZ_KEY, {"software_version": "5.0rc38"}))
        sensor.update_callback(
            make_bucket(TOPAZ_KEY, {"auto_away": True, "software_version": "5.0rc38"})
        )

    assert sensor.available is False


async def test_occupancy_state_follows_firmware_from_first_data_and_subscriber(
    hass, component_setup_with_cookies: ComponentSetup
):
    """A Protect updating to firmware 5 turns its occupancy sensor unavailable."""
    topaz = {
        "object_key": TOPAZ_KEY,
        "object_revision": 1,
        "object_timestamp": 1,
        "value": {
            "wired_or_battery": 0,
            "auto_away": False,
            "software_version": "3.6rc8",
        },
    }
    first_data = FirstDataAPIResponse(
        weather_for_structures={},
        service_urls={"urls": {"transport_url": "https://t.example.com"}},
        _2fa_enabled=False,
        updated_buckets=[topaz],
    )

    with (
        patch.object(NestSessionManager, "async_setup", return_value=first_data),
        patch("custom_components.nest_protect._async_subscribe_for_data"),
        patch("custom_components.nest_protect._async_observe_locks_loop"),
    ):
        await component_setup_with_cookies()

    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, OCCUPANCY_UNIQUE_ID
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == STATE_ON

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    entry_data: HomeAssistantNestProtectData = entry.runtime_data
    entry_data.client.nest_session = MagicMock()
    entry_data.client.subscribe_for_data = AsyncMock(
        return_value={
            "objects": [
                {
                    **topaz,
                    "object_revision": 2,
                    "value": {**topaz["value"], "software_version": "5.0rc31"},
                }
            ]
        }
    )

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(NestSessionManager, "ensure_session", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, first_data)
        await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
