"""Tests for thermostat discovery from the protobuf observe stream."""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect import (
    DOMAIN,
    HomeAssistantNestProtectData,
    _apply_protobuf_device_update,
    _async_restore_protobuf_thermostats,
)
from custom_components.nest_protect.pynest.models import Bucket
from custom_components.nest_protect.pynest.protobuf import (
    RCS_SOURCE_TYPE_BACKPLATE,
    RCS_SOURCE_TYPE_MULTI_SENSOR,
    RCS_SOURCE_TYPE_SINGLE_SENSOR,
    ProtobufDeviceUpdate,
)
from custom_components.nest_protect.sensor import (
    ACTIVE_TEMPERATURE_SENSOR_DESCRIPTION,
    SENSOR_DESCRIPTIONS,
    THERMOSTAT_SENSOR_KEYS,
    NestThermostatActiveSensor,
    NestThermostatSensor,
)
from custom_components.nest_protect.thermostat import (
    is_discoverable_thermostat,
    subscribe_to_thermostat_discovery,
    thermostat_discovery_signal,
)

THERMOSTAT_KEY = "device.09AB12"
PEER_DEVICE_VALUE = {
    "using_protobuf": True,
    "device_id": "09AB12",
    "structure_id": "legacy",
    "protobuf_device_type": "nest.resource.NestLearningThermostat3Resource",
}


def _entry_data(**kwargs) -> HomeAssistantNestProtectData:
    """Build a minimal HomeAssistantNestProtectData."""
    return HomeAssistantNestProtectData(
        devices=kwargs.pop("devices", {}),
        structures={},
        areas=kwargs.pop("areas", {}),
        client=MagicMock(),
        session_manager=MagicMock(),
        grpc_lock_client=MagicMock(),
        **kwargs,
    )


def _thermostat_bucket(value: dict | None = None) -> Bucket:
    """Build a thermostat bucket as protobuf discovery would."""
    return Bucket(
        object_key=THERMOSTAT_KEY,
        object_revision=0,
        object_timestamp=0,
        value={**PEER_DEVICE_VALUE, **(value or {})},
    )


async def test_thermostat_update_creates_bucket_and_defers_discovery(hass):
    """Test the bucket is created but discovery waits for the serial number."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry_data = _entry_data()
    discovered: list[Bucket] = []
    async_dispatcher_connect(
        hass, thermostat_discovery_signal(entry.entry_id), discovered.append
    )

    _apply_protobuf_device_update(
        hass,
        entry,
        entry_data,
        ProtobufDeviceUpdate(object_key=THERMOSTAT_KEY, value=PEER_DEVICE_VALUE),
    )
    await hass.async_block_till_done()

    assert entry_data.devices[THERMOSTAT_KEY].value == PEER_DEVICE_VALUE
    assert entry_data.devices[THERMOSTAT_KEY].type == "device"
    assert not discovered


async def test_thermostat_discovery_fires_once_identity_arrives(hass):
    """Test discovery fires on the identity trait, then updates go per-object."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry_data = _entry_data()
    discovered: list[Bucket] = []
    updated: list[Bucket] = []
    async_dispatcher_connect(
        hass, thermostat_discovery_signal(entry.entry_id), discovered.append
    )
    async_dispatcher_connect(hass, THERMOSTAT_KEY, updated.append)

    for value in (
        PEER_DEVICE_VALUE,
        {"serial_number": "thermostat-serial", "model": "Nest Learning Thermostat"},
        {"backplate_temperature": 21.5},
    ):
        _apply_protobuf_device_update(
            hass,
            entry,
            entry_data,
            ProtobufDeviceUpdate(object_key=THERMOSTAT_KEY, value=value),
        )
    await hass.async_block_till_done()

    assert len(discovered) == 1
    assert discovered[0].value["serial_number"] == "thermostat-serial"
    # The bucket is mutated in place, so the announced bucket carries the
    # temperature that arrived after discovery.
    assert len(updated) == 1
    assert updated[0] is discovered[0]
    assert discovered[0].value["backplate_temperature"] == 21.5


async def test_unknown_non_thermostat_update_is_dropped(hass):
    """Test devices other than thermostats still need a legacy bucket first."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry_data = _entry_data()

    _apply_protobuf_device_update(
        hass,
        entry,
        entry_data,
        ProtobufDeviceUpdate(
            object_key="kryptonite.18B430", value={"current_temperature": 21.5}
        ),
    )
    await hass.async_block_till_done()

    assert entry_data.devices == {}


async def test_subscribe_replays_already_discovered_thermostats(hass):
    """Test thermostats found before platform setup are replayed exactly once."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    bucket = _thermostat_bucket({"serial_number": "thermostat-serial"})
    topaz = Bucket(
        object_key="topaz.1234", object_revision=0, object_timestamp=0, value={}
    )
    entry_data = _entry_data(devices={THERMOSTAT_KEY: bucket, "topaz.1234": topaz})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data
    added: list[Bucket] = []

    subscribe_to_thermostat_discovery(
        hass,
        entry,
        lambda entities: None,
        lambda discovered: added.append(discovered) or [MagicMock()],
    )

    assert added == [bucket]

    # A late discovery signal for the same thermostat must not add it twice.
    async_dispatcher_send(hass, thermostat_discovery_signal(entry.entry_id), bucket)
    await hass.async_block_till_done()

    assert added == [bucket]


async def test_discovery_creates_the_thermostat_sensors(hass):
    """Test the sensor platform adds every thermostat reading in one go."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    bucket = _thermostat_bucket(
        {
            "serial_number": "thermostat-serial",
            "where_id": "where.living-room",
            "backplate_temperature": 21.51234,
            "current_humidity": 43.6,
        }
    )
    entry_data = _entry_data(
        devices={THERMOSTAT_KEY: bucket}, areas={"where.living-room": "Living Room"}
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data
    added: list[NestThermostatSensor] = []

    descriptions = [
        description
        for description in SENSOR_DESCRIPTIONS
        if description.key in THERMOSTAT_SENSOR_KEYS
    ]
    subscribe_to_thermostat_discovery(
        hass,
        entry,
        added.extend,
        lambda discovered: [
            NestThermostatSensor(discovered, description, entry_data.areas, MagicMock())
            for description in descriptions
        ],
    )

    by_key = {sensor.entity_description.key: sensor for sensor in added}
    assert set(by_key) == {
        "backplate_temperature",
        "current_temperature",
        "current_humidity",
    }
    assert by_key["backplate_temperature"].unique_id == (
        "device.09AB12-backplate_temperature"
    )
    assert by_key["backplate_temperature"].native_value == 21.51
    assert by_key["current_humidity"].native_value == 44
    # Not published yet — the entity is added up front and fills in later.
    assert by_key["current_temperature"].native_value is None
    assert by_key["current_temperature"].device_info["name"] == (
        "Nest Thermostat (Living Room)"
    )


def _active_sensor_entity(rcs: dict) -> NestThermostatActiveSensor:
    """Build the active-source sensor with one named temperature sensor known."""
    kryptonite = Bucket(
        object_key="kryptonite.18B430",
        object_revision=0,
        object_timestamp=0,
        value={"where_id": "where.hallway"},
    )
    bucket = _thermostat_bucket({"serial_number": "thermostat-serial", **rcs})

    return NestThermostatActiveSensor(
        bucket,
        ACTIVE_TEMPERATURE_SENSOR_DESCRIPTION,
        {"where.hallway": "Hallway"},
        MagicMock(),
        {THERMOSTAT_KEY: bucket, "kryptonite.18B430": kryptonite},
    )


def test_active_sensor_names_the_selected_remote_sensor():
    """Test a single selected sensor is reported by its room name."""
    sensor = _active_sensor_entity(
        {
            "rcs_source_type": RCS_SOURCE_TYPE_SINGLE_SENSOR,
            "active_rcs_sensors": ["kryptonite.18B430"],
            "associated_rcs_sensors": ["kryptonite.18B430", "kryptonite.29CD34"],
        }
    )

    assert sensor.native_value == "Hallway"
    assert sensor.extra_state_attributes == {
        "source_type": "single_sensor",
        "active_sensors": ["kryptonite.18B430"],
        "associated_sensors": ["kryptonite.18B430", "kryptonite.29CD34"],
    }


def test_active_sensor_reports_the_thermostat_itself():
    """Test the backplate source reads as the thermostat, not a sensor."""
    sensor = _active_sensor_entity(
        {
            "rcs_source_type": RCS_SOURCE_TYPE_BACKPLATE,
            "active_rcs_sensors": [],
            "associated_rcs_sensors": ["kryptonite.18B430"],
        }
    )

    assert sensor.native_value == "Thermostat"
    assert sensor.extra_state_attributes["source_type"] == "backplate"


def test_active_sensor_falls_back_to_the_device_id():
    """Test a selected sensor we have no bucket for still identifies itself."""
    sensor = _active_sensor_entity(
        {
            "rcs_source_type": RCS_SOURCE_TYPE_SINGLE_SENSOR,
            "active_rcs_sensors": ["kryptonite.29CD34"],
        }
    )

    assert sensor.native_value == "29CD34"


def test_active_sensor_lists_a_multi_sensor_group():
    """Test an averaged group names every sensor in it."""
    sensor = _active_sensor_entity(
        {
            "rcs_source_type": RCS_SOURCE_TYPE_MULTI_SENSOR,
            "active_rcs_sensors": ["kryptonite.29CD34", "kryptonite.18B430"],
        }
    )

    assert sensor.native_value == "29CD34, Hallway"
    assert sensor.extra_state_attributes["source_type"] == "multi_sensor"


def test_active_sensor_is_unknown_before_the_trait_arrives():
    """Test the entity is added up front and stays unknown until published."""
    sensor = _active_sensor_entity({})

    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {
        "source_type": "unknown",
        "active_sensors": [],
        "associated_sensors": [],
    }


async def test_active_sensor_name_resolves_when_the_sensor_traits_land(hass):
    """Test a sensor's room label arriving later replaces the bare device id."""
    kryptonite = Bucket(
        object_key="kryptonite.18B430",
        object_revision=0,
        object_timestamp=0,
        value={},
    )
    thermostat = _thermostat_bucket(
        {
            "serial_number": "thermostat-serial",
            "rcs_source_type": RCS_SOURCE_TYPE_SINGLE_SENSOR,
            "active_rcs_sensors": ["kryptonite.18B430"],
        }
    )
    areas = {"where.hallway": "Hallway"}
    sensor = NestThermostatActiveSensor(
        thermostat,
        ACTIVE_TEMPERATURE_SENSOR_DESCRIPTION,
        areas,
        MagicMock(),
        {THERMOSTAT_KEY: thermostat, "kryptonite.18B430": kryptonite},
    )
    sensor.hass = hass
    writes: list[str | None] = []
    sensor.async_write_ha_state = lambda: writes.append(sensor.native_value)

    # The selection is known before the sensor has published its location.
    await sensor.async_added_to_hass()
    assert sensor.native_value == "18B430"

    # DeviceLocatedSettingsTrait lands on the sensor's own bucket.
    kryptonite.value["where_id"] = "where.hallway"
    async_dispatcher_send(hass, "kryptonite.18B430", kryptonite)
    await hass.async_block_till_done()

    assert writes == ["Hallway"]
    assert sensor.native_value == "Hallway"


async def test_active_sensor_stops_following_a_deselected_sensor(hass):
    """Test subscriptions follow the selection instead of accumulating."""
    thermostat = _thermostat_bucket(
        {
            "serial_number": "thermostat-serial",
            "rcs_source_type": RCS_SOURCE_TYPE_SINGLE_SENSOR,
            "active_rcs_sensors": ["kryptonite.18B430"],
        }
    )
    sensor = NestThermostatActiveSensor(
        thermostat,
        ACTIVE_TEMPERATURE_SENSOR_DESCRIPTION,
        {},
        MagicMock(),
        {THERMOSTAT_KEY: thermostat},
    )
    sensor.hass = hass
    sensor.async_write_ha_state = MagicMock()

    await sensor.async_added_to_hass()
    assert set(sensor._sensor_unsubs) == {"kryptonite.18B430"}

    # The thermostat switches to its own sensor.
    thermostat.value.update(
        {"rcs_source_type": RCS_SOURCE_TYPE_BACKPLATE, "active_rcs_sensors": []}
    )
    sensor.update_callback(thermostat)

    assert sensor._sensor_unsubs == {}
    assert sensor.native_value == "Thermostat"


async def test_cached_thermostats_are_restored_before_platform_setup(hass):
    """Test a reload recreates thermostat entities without waiting for the stream."""
    store = MagicMock()
    store.async_load = AsyncMock(
        return_value={
            THERMOSTAT_KEY: {
                **PEER_DEVICE_VALUE,
                "serial_number": "thermostat-serial",
                "model": "Nest Learning Thermostat",
            },
            # Never cached in practice, but must not be resurrected as a device.
            "kryptonite.18B430": {"serial_number": "sensor-serial"},
        }
    )
    entry_data = _entry_data(device_store=store)

    await _async_restore_protobuf_thermostats(entry_data)

    assert set(entry_data.devices) == {THERMOSTAT_KEY}
    assert entry_data.devices[THERMOSTAT_KEY].value["model"] == (
        "Nest Learning Thermostat"
    )
    # Marked as announced, so live traits update the entity instead of
    # triggering a second discovery.
    assert entry_data.protobuf_thermostats == {THERMOSTAT_KEY}


async def test_incomplete_cache_entries_are_ignored(hass):
    """Test a cached thermostat without a serial number is not restored."""
    store = MagicMock()
    store.async_load = AsyncMock(return_value={THERMOSTAT_KEY: PEER_DEVICE_VALUE})
    entry_data = _entry_data(device_store=store)

    await _async_restore_protobuf_thermostats(entry_data)

    assert entry_data.devices == {}
    assert entry_data.protobuf_thermostats == set()


async def test_identity_updates_are_cached_but_readings_are_not(hass):
    """Test only identity fields are persisted, and only when they change."""
    entry = MockConfigEntry(domain=DOMAIN)
    store = MagicMock()
    entry_data = _entry_data(device_store=store)

    for value in (
        PEER_DEVICE_VALUE,
        {"serial_number": "thermostat-serial", "model": "Nest Learning Thermostat"},
    ):
        _apply_protobuf_device_update(
            hass,
            entry,
            entry_data,
            ProtobufDeviceUpdate(object_key=THERMOSTAT_KEY, value=value),
        )

    assert store.async_delay_save.call_count == 2
    assert store.async_delay_save.call_args[0][0]() == {
        THERMOSTAT_KEY: {
            **PEER_DEVICE_VALUE,
            "serial_number": "thermostat-serial",
            "model": "Nest Learning Thermostat",
        }
    }

    # A temperature update carries nothing worth persisting.
    _apply_protobuf_device_update(
        hass,
        entry,
        entry_data,
        ProtobufDeviceUpdate(
            object_key=THERMOSTAT_KEY, value={"backplate_temperature": 21.5}
        ),
    )

    assert store.async_delay_save.call_count == 2


async def test_late_identity_traits_update_the_device_registry(hass):
    """Test model/room traits arriving after the entity is added rename the device."""
    bucket = _thermostat_bucket({"serial_number": "thermostat-serial"})
    areas = {"where.living-room": "Living Room"}
    description = next(
        d for d in SENSOR_DESCRIPTIONS if d.key == "backplate_temperature"
    )
    sensor = NestThermostatSensor(bucket, description, areas, MagicMock())
    sensor.hass = hass
    sensor.async_write_ha_state = MagicMock()

    assert sensor.device_info["name"] == "Nest Thermostat"

    # The observe loop mutates the bucket in place before dispatching it, so the
    # entity cannot recover the previous values from the bucket it receives.
    bucket.value.update(
        {
            "model": "Nest Learning Thermostat",
            "current_version": "6.2.2",
            "where_id": "where.living-room",
        }
    )

    device_entry = MagicMock(id="device-entry-id")
    with (
        patch.object(
            type(sensor), "device_entry", PropertyMock(return_value=device_entry)
        ),
        patch("custom_components.nest_protect.sensor.dr.async_get") as async_get,
    ):
        sensor.update_callback(bucket)

        async_get.return_value.async_update_device.assert_called_once_with(
            "device-entry-id",
            name="Nest Thermostat (Living Room)",
            model="Nest Learning Thermostat",
            sw_version="6.2.2",
        )

        # A temperature-only update must not touch the registry again.
        async_get.return_value.async_update_device.reset_mock()
        bucket.value["backplate_temperature"] = 21.5
        sensor.update_callback(bucket)

        async_get.return_value.async_update_device.assert_not_called()


async def test_incomplete_thermostat_is_not_discoverable():
    """Test a thermostat without a serial number is not ready to be added."""
    assert not is_discoverable_thermostat(_thermostat_bucket())
    assert is_discoverable_thermostat(
        _thermostat_bucket({"serial_number": "thermostat-serial"})
    )
    assert not is_discoverable_thermostat(
        Bucket(
            object_key="kryptonite.18B430",
            object_revision=0,
            object_timestamp=0,
            value={"serial_number": "sensor-serial"},
        )
    )
