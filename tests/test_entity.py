"""Tests for the Nest Protect entity base class."""

from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import EntityDescription

from custom_components.nest_protect.const import DOMAIN
from custom_components.nest_protect.entity import NestEntity
from custom_components.nest_protect.pynest.models import Bucket

AREAS = {"where.living-room": "Living Room"}

# Topaz-2.0 revisions omit wifi_mac_address, serial_number and software_version
MINIMAL_TOPAZ = {"where_id": "where.living-room"}

COMPLETE_TOPAZ = {
    "where_id": "where.living-room",
    "wifi_mac_address": "AA:BB:CC:DD:EE:FF",
    "serial_number": "ABC123",
    "model": "Topaz-2.9",
    "software_version": "3.5.0",
    "wired_or_battery": 1,
    "structure_id": "structure.4321",
}


def build_entity(value: dict, object_key: str = "topaz.1234") -> NestEntity:
    """Build a NestEntity from a raw bucket value."""
    bucket = Bucket(
        object_key=object_key,
        object_revision=1,
        object_timestamp=1,
        value=value,
    )
    return NestEntity(
        bucket=bucket,
        description=EntityDescription(key="test"),
        areas=AREAS,
        client=MagicMock(),
    )


def test_complete_topaz_device_info():
    """Test that a fully populated Topaz reports every field."""
    device_info = build_entity(COMPLETE_TOPAZ).device_info

    assert device_info["connections"] == {
        (dr.CONNECTION_NETWORK_MAC, "AA:BB:CC:DD:EE:FF")
    }
    assert device_info["identifiers"] == {(DOMAIN, "ABC123")}
    assert device_info["name"] == "Nest Protect (Living Room)"
    assert device_info["model"] == "Topaz-2.9"
    assert device_info["sw_version"] == "3.5.0"
    assert device_info["hw_version"] == "Battery"
    assert device_info["suggested_area"] == "Living Room"
    assert device_info["configuration_url"] == (
        "https://home.nest.com/protect/structure.4321/settings/device/1234#about"
    )


def test_incomplete_topaz_does_not_raise():
    """Test that a Topaz missing optional fields still produces device info."""
    device_info = build_entity(MINIMAL_TOPAZ).device_info

    assert device_info is not None
    # No MAC means no connection, rather than a connection to None
    assert device_info["connections"] == set()
    assert device_info["identifiers"] == {(DOMAIN, "topaz.1234")}
    assert device_info["name"] == "Nest Protect (Living Room)"
    assert device_info["model"] is None
    assert device_info["sw_version"] is None
    assert device_info["configuration_url"] is None


def test_incomplete_topaz_power_source_is_unknown():
    """Test that an absent wired_or_battery is not reported as Battery."""
    assert build_entity(MINIMAL_TOPAZ).device_info["hw_version"] is None


@pytest.mark.parametrize(
    ("wired_or_battery", "expected"),
    [(0, "Wired"), (1, "Battery")],
)
def test_topaz_power_source(wired_or_battery: int, expected: str):
    """Test that a known wired_or_battery maps to a hardware version."""
    entity = build_entity(MINIMAL_TOPAZ | {"wired_or_battery": wired_or_battery})
    assert entity.device_info["hw_version"] == expected


def test_missing_where_id_does_not_raise():
    """Test that a payload without where_id still sets up."""
    entity = build_entity({})

    assert entity.area is None
    assert entity.device_info["name"] == "Nest Protect"
    assert entity.device_info["suggested_area"] is None


def test_description_takes_precedence_over_area():
    """Test that the device description is preferred as a label."""
    entity = build_entity(MINIMAL_TOPAZ | {"description": "Hallway"})
    assert entity.device_info["name"] == "Nest Protect (Hallway)"


def test_kryptonite_device_info():
    """Test that a temperature sensor reports its own device info."""
    device_info = build_entity(
        {
            "where_id": "where.living-room",
            "serial_number": "XYZ789",
            "model": "Kryp-1.0",
        },
        object_key="kryptonite.5678",
    ).device_info

    assert device_info["identifiers"] == {(DOMAIN, "XYZ789")}
    assert device_info["name"] == "Nest Temperature Sensor (Living Room)"
    assert device_info["model"] == "Kryp-1.0"


def test_area_falls_back_to_the_protobuf_where_label():
    """Test the room name published by DeviceLocatedSettingsTrait is used.

    Thermostats have no `where.` REST bucket, so a room that never appears in
    `areas` can still be named from the trait's own literal.
    """
    entity = build_entity(
        {"where_id": "where.unmapped", "where_label": "Guest room"},
        object_key="device.09AB12",
    )

    assert entity.area == "Guest room"
    assert entity.device_info["name"] == "Nest Thermostat (Guest room)"


def test_area_prefers_the_mapped_where_id_over_the_label():
    """Test the user-facing `where.` bucket name wins when it resolves."""
    entity = build_entity(
        {"where_id": "where.living-room", "where_label": "Stale label"},
        object_key="device.09AB12",
    )

    assert entity.area == "Living Room"


def test_incomplete_kryptonite_falls_back_to_object_key():
    """Test that a temperature sensor without a serial number still sets up."""
    device_info = build_entity({}, object_key="kryptonite.5678").device_info

    assert device_info["identifiers"] == {(DOMAIN, "kryptonite.5678")}
    assert device_info["name"] == "Nest Temperature Sensor"


def test_thermostat_device_info():
    """Test that a protobuf-discovered thermostat reports its own device info."""
    device_info = build_entity(
        {
            "where_id": "where.living-room",
            "serial_number": "THERM123",
            "model": "Nest Learning Thermostat",
            "current_version": "6.2.2",
        },
        object_key="device.09AB12",
    ).device_info

    assert device_info["identifiers"] == {(DOMAIN, "THERM123")}
    assert device_info["name"] == "Nest Thermostat (Living Room)"
    assert device_info["model"] == "Nest Learning Thermostat"
    assert device_info["sw_version"] == "6.2.2"


def test_incomplete_thermostat_falls_back_to_object_key():
    """Test that a thermostat without identity traits still sets up."""
    device_info = build_entity({}, object_key="device.09AB12").device_info

    assert device_info["identifiers"] == {(DOMAIN, "device.09AB12")}
    assert device_info["name"] == "Nest Thermostat"
    assert device_info["model"] is None
