"""Tests for small protobuf encoders."""

from struct import pack

from custom_components.nest_protect.pynest.protobuf import (
    BATTERY_TYPE_URL,
    DEVICE_IDENTITY_TYPE_URL,
    DEVICE_LOCATED_SETTINGS_TYPE_URL,
    LIVENESS_TYPE_URL,
    NEST_KRYPTONITE_RESOURCE,
    PEER_DEVICES_TYPE_URL,
    RCS_SETTINGS_TYPE_URL,
    STRUCTURE_INFO_TYPE_URL,
    STRUCTURE_MODE_CHANGE_TYPE_URL,
    STRUCTURE_MODE_TYPE_URL,
    TEMPERATURE_TYPE_URL,
    USER_INFO_TYPE_URL,
    ProtobufDeviceUpdate,
    ProtobufObserveState,
    _field_bytes,
    _field_string,
    _field_varint,
    decode_observe_stream_frames,
    decode_structure_updates,
    encode_observe_request,
    encode_structure_mode_change_request,
    encode_structure_mode_resource_command_request,
)

THERMOSTAT_RESOURCE = "nest.resource.NestLearningThermostat3Resource"


def test_encode_structure_mode_change_request_home():
    """Test encoding StructureModeChangeRequest for Home."""
    result = encode_structure_mode_change_request(home=True, user_id="USER_123")

    assert result == b"\x08\x01\x10\x01\x1a\n\n\x08USER_123"


def test_encode_structure_mode_change_request_away():
    """Test encoding StructureModeChangeRequest for Away."""
    result = encode_structure_mode_change_request(home=False, user_id="USER_123")

    assert result == b"\x08\x02\x10\x01\x1a\n\n\x08USER_123"


def test_encode_structure_mode_resource_command_request():
    """Test encoding ResourceCommandRequest shape."""
    result = encode_structure_mode_resource_command_request(
        structure_resource_id="STRUCTURE_abc",
        home=False,
        user_id="USER_123",
    )

    assert b"STRUCTURE_abc" in result
    assert b"structure_mode" in result
    assert STRUCTURE_MODE_CHANGE_TYPE_URL.encode() in result
    assert b"\x08\x02\x10\x01\x1a\n\n\x08USER_123" in result


def test_encode_observe_request_asks_for_home_away_traits():
    """Test encoding the observe request trait list."""
    result = encode_observe_request()

    assert b"nest.trait.user.UserInfoTrait" in result
    assert b"nest.trait.structure.StructureInfoTrait" in result
    assert b"nest.trait.occupancy.StructureModeTrait" in result
    assert b"weave.trait.peerdevices.PeerDevicesTrait" in result
    assert b"nest.trait.sensor.TemperatureTrait" in result
    assert b"nest.trait.hvac.RemoteComfortSensingSettingsTrait" in result


def test_decode_observe_stream_frames():
    """Test splitting observe stream frames across chunks."""
    first = _field_bytes(1, b"first")
    second = _field_bytes(1, b"second")
    payload = first + second

    frames, pending = decode_observe_stream_frames(payload[:-2])
    assert frames == [first]
    assert pending

    frames, pending = decode_observe_stream_frames(pending + payload[-2:])
    assert frames == [second]
    assert pending == b""


def test_decode_structure_updates_maps_structure_and_mode():
    """Test decoding Homebridge-equivalent structure info and mode traits."""
    state = ProtobufObserveState()
    payload = _stream_body(
        _get_property(
            "USER_123",
            USER_INFO_TYPE_URL,
            _field_string(1, "user.legacy"),
        ),
        _get_property(
            "STRUCTURE_new",
            STRUCTURE_INFO_TYPE_URL,
            _field_string(1, "structure.legacy"),
        ),
        _get_property(
            "STRUCTURE_new",
            STRUCTURE_MODE_TYPE_URL,
            _field_varint(1, 2),
        ),
    )

    updates = decode_structure_updates(payload, state)

    assert updates[0].resource_id == "STRUCTURE_new"
    assert updates[0].legacy_structure_id == "legacy"
    assert updates[0].user_id == "USER_123"
    assert updates[0].away is None
    assert updates[1].resource_id == "STRUCTURE_new"
    assert updates[1].legacy_structure_id == "legacy"
    assert updates[1].user_id == "USER_123"
    assert updates[1].away is True


def test_decode_structure_updates_home_mode():
    """Test decoding structure mode Home."""
    state = ProtobufObserveState(
        user_id="USER_123",
        legacy_structure_ids={"STRUCTURE_new": "legacy"},
    )
    payload = _stream_body(
        _get_property(
            "STRUCTURE_new",
            STRUCTURE_MODE_TYPE_URL,
            _field_varint(1, 1),
        )
    )

    updates = decode_structure_updates(payload, state)

    assert updates[0].away is False


def test_decode_kryptonite_peer_devices_and_traits():
    """Test decoding Homebridge-equivalent Kryptonite protobuf traits."""
    state = ProtobufObserveState(user_id="USER_123")
    payload = _stream_body(
        _get_property(
            "STRUCTURE_new",
            STRUCTURE_INFO_TYPE_URL,
            _field_string(1, "structure.legacy"),
        ),
        _get_property(
            "STRUCTURE_new",
            PEER_DEVICES_TYPE_URL,
            _field_bytes(
                1,
                _field_bytes(
                    2,
                    _field_bytes(1, _field_string(1, "DEVICE_18B430"))
                    + _field_bytes(2, _field_string(1, NEST_KRYPTONITE_RESOURCE))
                    + _field_string(5, "1.2.3"),
                ),
            ),
        ),
        _get_property(
            "18B430",
            TEMPERATURE_TYPE_URL,
            _field_bytes(1, _field_bytes(1, _field_float(1, 21.5))),
        ),
        _get_property(
            "18B430",
            DEVICE_IDENTITY_TYPE_URL,
            _field_bytes(4, _field_string(1, "Nest Temperature Sensor"))
            + _field_string(6, "serial")
            + _field_string(7, "1.2.4"),
        ),
        _get_property(
            "18B430",
            DEVICE_LOCATED_SETTINGS_TYPE_URL,
            _field_bytes(2, _field_string(1, "where-1"))
            + _field_bytes(4, _field_varint(1, 4)),
        ),
        _get_property(
            "18B430",
            LIVENESS_TYPE_URL,
            _field_varint(1, 1),
        ),
        _get_property(
            "18B430",
            BATTERY_TYPE_URL,
            _field_varint(32, 1)
            + _field_bytes(33, _field_bytes(1, _field_float(1, 87.5))),
        ),
    )

    updates = decode_structure_updates(payload, state)
    device_updates = [
        update for update in updates if isinstance(update, ProtobufDeviceUpdate)
    ]

    assert device_updates[0].object_key == "kryptonite.18B430"
    assert device_updates[0].value == {
        "using_protobuf": True,
        "device_id": "18B430",
        "structure_id": "legacy",
        "current_version": "1.2.3",
        "user_id": "USER_123",
        "protobuf_device_type": NEST_KRYPTONITE_RESOURCE,
    }
    assert device_updates[1].value["current_temperature"] == 21.5
    assert device_updates[2].value == {
        "model": "Nest Temperature Sensor",
        "serial_number": "serial",
        "current_version": "1.2.4",
    }
    assert device_updates[3].value == {"where_id": "where-1", "fixture_type": 4}
    assert device_updates[4].value == {"is_online": True}
    assert device_updates[5].value == {"battery_status": 1, "battery_level": 87.5}


def test_decode_thermostat_peer_device_and_temperatures():
    """Test a thermostat mounts as device.<id> with both temperature traits."""
    state = ProtobufObserveState(user_id="USER_123")
    payload = _stream_body(
        _get_property(
            "STRUCTURE_new",
            STRUCTURE_INFO_TYPE_URL,
            _field_string(1, "structure.legacy"),
        ),
        _get_property(
            "STRUCTURE_new",
            PEER_DEVICES_TYPE_URL,
            _field_bytes(
                1,
                _field_bytes(
                    2,
                    _field_bytes(1, _field_string(1, "DEVICE_09AB12"))
                    + _field_bytes(2, _field_string(1, THERMOSTAT_RESOURCE))
                    + _field_string(5, "6.2.1"),
                ),
            ),
        ),
        _get_property(
            "09AB12",
            TEMPERATURE_TYPE_URL,
            _field_bytes(1, _field_bytes(1, _field_float(1, 21.5))),
            trait_label="backplate_temperature",
        ),
        _get_property(
            "09AB12",
            TEMPERATURE_TYPE_URL,
            _field_bytes(1, _field_bytes(1, _field_float(1, 19.0))),
            trait_label="current_temperature",
        ),
        _get_property(
            "09AB12",
            DEVICE_IDENTITY_TYPE_URL,
            _field_bytes(4, _field_string(1, "Nest Learning Thermostat"))
            + _field_string(6, "thermostat-serial")
            + _field_string(7, "6.2.2"),
        ),
    )

    updates = decode_structure_updates(payload, state)
    device_updates = [
        update for update in updates if isinstance(update, ProtobufDeviceUpdate)
    ]

    assert device_updates[0].object_key == "device.09AB12"
    assert device_updates[0].value == {
        "using_protobuf": True,
        "device_id": "09AB12",
        "structure_id": "legacy",
        "current_version": "6.2.1",
        "user_id": "USER_123",
        "protobuf_device_type": THERMOSTAT_RESOURCE,
    }
    # Both traits are nest.trait.sensor.TemperatureTrait; only the label differs.
    assert device_updates[1].object_key == "device.09AB12"
    assert device_updates[1].value == {"backplate_temperature": 21.5}
    assert device_updates[2].value == {"current_temperature": 19.0}
    assert device_updates[3].value == {
        "model": "Nest Learning Thermostat",
        "serial_number": "thermostat-serial",
        "current_version": "6.2.2",
    }


def test_decode_rcs_settings_single_sensor():
    """Test the selected remote comfort sensor is decoded."""
    state = ProtobufObserveState(device_types={"09AB12": THERMOSTAT_RESOURCE})
    payload = _stream_body(
        _get_property(
            "09AB12",
            RCS_SETTINGS_TYPE_URL,
            _field_varint(1, 3)  # rcsControlMode: SCHEDULE_OVERRIDE
            + _field_bytes(
                2,
                _field_varint(1, 2)  # rcsSourceType: SINGLE_SENSOR
                + _field_bytes(2, _field_string(1, "DEVICE_18B430")),
            )
            + _field_bytes(4, _field_bytes(1, _field_string(1, "DEVICE_18B430")))
            + _field_bytes(4, _field_bytes(1, _field_string(1, "DEVICE_29CD34"))),
            trait_label="remote_comfort_sensing_settings",
        )
    )

    updates = decode_structure_updates(payload, state)

    assert updates[0].value == {
        "rcs_control_mode": 3,
        "rcs_source_type": 2,
        "active_rcs_sensors": ["kryptonite.18B430"],
        "associated_rcs_sensors": ["kryptonite.18B430", "kryptonite.29CD34"],
    }


def test_decode_rcs_settings_backplate_clears_selection():
    """Test falling back to the thermostat's own sensor empties the selection."""
    state = ProtobufObserveState(device_types={"09AB12": THERMOSTAT_RESOURCE})
    payload = _stream_body(
        _get_property(
            "09AB12",
            RCS_SETTINGS_TYPE_URL,
            # rcsSourceType BACKPLATE, with activeRcsSensor cleared.
            _field_bytes(2, _field_varint(1, 1))
            + _field_bytes(4, _field_bytes(1, _field_string(1, "DEVICE_18B430"))),
            trait_label="remote_comfort_sensing_settings",
        )
    )

    updates = decode_structure_updates(payload, state)

    assert updates[0].value == {
        "rcs_control_mode": None,
        "rcs_source_type": 1,
        "active_rcs_sensors": [],
        "associated_rcs_sensors": ["kryptonite.18B430"],
    }


def test_decode_rcs_settings_multi_sensor():
    """Test a multi-sensor average reports the whole group as active."""
    state = ProtobufObserveState(device_types={"09AB12": THERMOSTAT_RESOURCE})
    payload = _stream_body(
        _get_property(
            "09AB12",
            RCS_SETTINGS_TYPE_URL,
            _field_bytes(2, _field_varint(1, 3))  # rcsSourceType: MULTI_SENSOR
            + _field_bytes(
                5,
                _field_varint(1, 1)  # multiSensorEnabled
                + _field_bytes(2, _field_string(1, "DEVICE_18B430"))
                + _field_bytes(2, _field_string(1, "DEVICE_29CD34")),
            ),
            trait_label="remote_comfort_sensing_settings",
        )
    )

    updates = decode_structure_updates(payload, state)

    assert updates[0].value["active_rcs_sensors"] == [
        "kryptonite.18B430",
        "kryptonite.29CD34",
    ]


def test_decode_skips_bucketized_trait_labels():
    """Test bucketized temperature history is ignored."""
    state = ProtobufObserveState(device_types={"09AB12": THERMOSTAT_RESOURCE})
    payload = _stream_body(
        _get_property(
            "09AB12",
            TEMPERATURE_TYPE_URL,
            _field_bytes(1, _field_bytes(1, _field_float(1, 21.5))),
            trait_label="backplate_temperature_bucketized",
        )
    )

    assert decode_structure_updates(payload, state) == []


def test_decode_ignores_unsupported_peer_device_types():
    """Test peer devices we have no bucket mapping for are skipped."""
    state = ProtobufObserveState(
        user_id="USER_123",
        legacy_structure_ids={"STRUCTURE_new": "legacy"},
    )
    payload = _stream_body(
        _get_property(
            "STRUCTURE_new",
            PEER_DEVICES_TYPE_URL,
            _field_bytes(
                1,
                _field_bytes(
                    2,
                    _field_bytes(1, _field_string(1, "DEVICE_CAM01"))
                    + _field_bytes(
                        2, _field_string(1, "nest.resource.NestCamIndoorResource")
                    ),
                ),
            ),
        )
    )

    assert decode_structure_updates(payload, state) == []


def _stream_body(*get_properties: bytes) -> bytes:
    return _field_bytes(1, b"".join(_field_bytes(3, get) for get in get_properties))


def _get_property(
    resource_id: str,
    type_url: str,
    value: bytes,
    trait_label: str | None = None,
) -> bytes:
    object_id = _field_string(1, resource_id)
    if trait_label is not None:
        object_id += _field_string(2, trait_label)
    any_payload = _field_string(1, type_url) + _field_bytes(2, value)
    indirect = _field_bytes(1, any_payload)
    return _field_bytes(1, object_id) + _field_bytes(3, indirect)


def _field_float(field_number: int, value: float) -> bytes:
    return bytes([(field_number << 3) | 5]) + pack("<f", value)
