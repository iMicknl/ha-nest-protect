"""Tests for small protobuf encoders."""

from struct import pack

from custom_components.nest_protect.pynest.protobuf import (
    BATTERY_TYPE_URL,
    DEVICE_IDENTITY_TYPE_URL,
    DEVICE_LOCATED_SETTINGS_TYPE_URL,
    HUMIDITY_TYPE_URL,
    LIVENESS_TYPE_URL,
    MAX_PENDING_DEVICES,
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
    assert b"nest.trait.sensor.HumidityTrait" in result
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


def test_decode_observe_stream_frames_keeps_a_split_varint():
    """Test a length prefix split across chunks is not treated as a desync."""
    # Field 1, length 200 -> a two-byte varint length the first chunk cuts.
    frame = _field_bytes(1, b"x" * 200)

    frames, pending = decode_observe_stream_frames(frame[:2])
    assert frames == []
    assert pending == frame[:2]

    frames, pending = decode_observe_stream_frames(pending + frame[2:])
    assert frames == [frame]
    assert pending == b""


def test_decode_observe_stream_frames_resyncs_on_bad_wire_type():
    """Test unparseable bytes are dropped rather than poisoning every chunk.

    Keeping them meant the parser could never resynchronise, silently wedging
    the observe stream until it reconnected.
    """
    good = _field_bytes(1, b"first")
    # Field 1, wire type 0 (varint) — never emitted at StreamBody level.
    garbage = _field_varint(1, 7)

    frames, pending = decode_observe_stream_frames(good + garbage)

    assert frames == [good]
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
            _located_settings(
                annotation_rid="ANNOTATION_BEDROOM",
                label="Kids room",
                legacy_uuid="where-1",
                fixture_type=4,
            ),
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
    assert device_updates[3].value == {
        "where_id": "where-1",
        "where_label": "Kids room",
        "where_annotation_rid": "ANNOTATION_BEDROOM",
        "fixture_type": 4,
    }
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


def test_decode_thermostat_humidity():
    """Test HumidityTrait decodes with the same nesting as temperature."""
    state = ProtobufObserveState(device_types={"09AB12": THERMOSTAT_RESOURCE})
    payload = _stream_body(
        _get_property(
            "09AB12",
            HUMIDITY_TYPE_URL,
            _field_bytes(1, _field_bytes(1, _field_float(1, 43.5))),
            trait_label="current_humidity",
        )
    )

    updates = decode_structure_updates(payload, state)

    assert updates[0].object_key == "device.09AB12"
    assert updates[0].value == {"current_humidity": 43.5}


def test_decode_thermostat_humidity_separates_backplate_from_current():
    """Test the paired humidity traits do not overwrite each other."""
    state = ProtobufObserveState(device_types={"09AB12": THERMOSTAT_RESOURCE})
    payload = _stream_body(
        _get_property(
            "09AB12",
            HUMIDITY_TYPE_URL,
            _field_bytes(1, _field_bytes(1, _field_float(1, 43.5))),
            trait_label="backplate_humidity",
        ),
        _get_property(
            "09AB12",
            HUMIDITY_TYPE_URL,
            _field_bytes(1, _field_bytes(1, _field_float(1, 61.0))),
            trait_label="current_humidity",
        ),
    )

    updates = decode_structure_updates(payload, state)

    assert updates[0].value == {"backplate_humidity": 43.5}
    assert updates[1].value == {"current_humidity": 61.0}


def test_decode_ignores_unknown_sensor_trait_labels():
    """Test an unrecognised label cannot overwrite the ambient reading."""
    state = ProtobufObserveState(device_types={"09AB12": THERMOSTAT_RESOURCE})
    payload = _stream_body(
        _get_property(
            "09AB12",
            TEMPERATURE_TYPE_URL,
            _field_bytes(1, _field_bytes(1, _field_float(1, 21.5))),
            trait_label="some_future_temperature",
        ),
        _get_property(
            "09AB12",
            HUMIDITY_TYPE_URL,
            _field_bytes(1, _field_bytes(1, _field_float(1, 43.5))),
            trait_label="some_future_humidity",
        ),
    )

    assert decode_structure_updates(payload, state) == []


def test_decode_located_settings_keeps_legacy_uuid_out_of_annotation_rid():
    """Test only whereLegacyUuid is written to where_id.

    The annotation rid is a protobuf-only identifier; writing it to `where_id`
    made the room lookup miss and the label fall back to the raw device id.
    """
    state = ProtobufObserveState(device_types={"18B430": NEST_KRYPTONITE_RESOURCE})
    payload = _stream_body(
        _get_property(
            "18B430",
            DEVICE_LOCATED_SETTINGS_TYPE_URL,
            _located_settings(annotation_rid="ANNOTATION_BEDROOM"),
        )
    )

    updates = decode_structure_updates(payload, state)

    assert updates[0].value == {"where_annotation_rid": "ANNOTATION_BEDROOM"}
    assert "where_id" not in updates[0].value


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


def test_decode_replays_device_traits_that_arrive_before_peer_devices():
    """Test a reading published before the device is mapped is not lost.

    PeerDevicesTrait is the only thing that maps a device id to a type, and the
    gateway gives no ordering guarantee against the per-device traits. Dropping
    the early ones left the entity unknown until the device happened to
    republish — tens of minutes for temperature, hours for RCS settings.
    """
    state = ProtobufObserveState(user_id="USER_123")
    payload = _stream_body(
        _get_property(
            "DEVICE_09AB12",
            TEMPERATURE_TYPE_URL,
            _field_bytes(1, _field_bytes(1, _field_float(1, 21.5))),
            trait_label="current_temperature",
        ),
        _get_property(
            "STRUCTURE_new",
            STRUCTURE_INFO_TYPE_URL,
            _field_string(1, "structure.legacy"),
        ),
        _get_property(
            "STRUCTURE_new",
            PEER_DEVICES_TYPE_URL,
            _peer_devices(("DEVICE_09AB12", THERMOSTAT_RESOURCE)),
        ),
    )

    updates = decode_structure_updates(payload, state)
    device_updates = [
        update for update in updates if isinstance(update, ProtobufDeviceUpdate)
    ]

    assert device_updates[0].value["protobuf_device_type"] == THERMOSTAT_RESOURCE
    assert device_updates[1].object_key == "device.09AB12"
    assert device_updates[1].value == {"current_temperature": 21.5}
    assert state.pending_device_traits == {}


def test_decode_replays_device_traits_held_across_frames():
    """Test a trait parked in one frame is released by a later frame."""
    state = ProtobufObserveState(user_id="USER_123")

    early = decode_structure_updates(
        _stream_body(
            _get_property(
                "DEVICE_18B430",
                TEMPERATURE_TYPE_URL,
                _field_bytes(1, _field_bytes(1, _field_float(1, 19.0))),
            )
        ),
        state,
    )
    assert early == []
    assert "18B430" in state.pending_device_traits

    updates = decode_structure_updates(
        _stream_body(
            _get_property(
                "STRUCTURE_new",
                STRUCTURE_INFO_TYPE_URL,
                _field_string(1, "structure.legacy"),
            ),
            _get_property(
                "STRUCTURE_new",
                PEER_DEVICES_TYPE_URL,
                _peer_devices(("DEVICE_18B430", NEST_KRYPTONITE_RESOURCE)),
            ),
        ),
        state,
    )

    assert updates[-1].object_key == "kryptonite.18B430"
    assert updates[-1].value == {"current_temperature": 19.0}


def test_decode_replays_peer_devices_that_arrive_before_structure_info():
    """Test peer devices held until the structure has a legacy id.

    Dropping the list here left every device on that structure unmapped for the
    life of the stream, which is the all-sensors-unknown case after a restart.
    """
    state = ProtobufObserveState(user_id="USER_123")
    payload = _stream_body(
        _get_property(
            "DEVICE_09AB12",
            TEMPERATURE_TYPE_URL,
            _field_bytes(1, _field_bytes(1, _field_float(1, 21.5))),
            trait_label="current_temperature",
        ),
        _get_property(
            "STRUCTURE_new",
            PEER_DEVICES_TYPE_URL,
            _peer_devices(("DEVICE_09AB12", THERMOSTAT_RESOURCE)),
        ),
        _get_property(
            "STRUCTURE_new",
            STRUCTURE_INFO_TYPE_URL,
            _field_string(1, "structure.legacy"),
        ),
    )

    updates = decode_structure_updates(payload, state)
    device_updates = [
        update for update in updates if isinstance(update, ProtobufDeviceUpdate)
    ]

    assert device_updates[0].value["structure_id"] == "legacy"
    assert device_updates[1].value == {"current_temperature": 21.5}
    assert state.pending_peer_devices == {}
    assert state.pending_device_traits == {}


def test_decode_drops_traits_for_devices_known_to_be_unsupported():
    """Test an unsupported device's traits are dropped, not held.

    A Protect or Guard on the protobuf stream publishes constantly; parking
    every one of those would keep the buffer churning for the life of the run.
    """
    state = ProtobufObserveState(
        user_id="USER_123",
        legacy_structure_ids={"STRUCTURE_new": "legacy"},
    )
    decode_structure_updates(
        _stream_body(
            _get_property(
                "STRUCTURE_new",
                PEER_DEVICES_TYPE_URL,
                _peer_devices(("DEVICE_CAM01", "nest.resource.NestCamIndoorResource")),
            )
        ),
        state,
    )
    assert state.unsupported_devices == {"CAM01"}

    updates = decode_structure_updates(
        _stream_body(
            _get_property(
                "DEVICE_CAM01",
                TEMPERATURE_TYPE_URL,
                _field_bytes(1, _field_bytes(1, _field_float(1, 21.5))),
            )
        ),
        state,
    )

    assert updates == []
    assert state.pending_device_traits == {}


def test_pending_device_traits_supersede_rather_than_accumulate():
    """Test repeated publishes by an unmapped device don't grow the buffer.

    The stream is meant to stay up for weeks; a device that never gets mapped
    must not be able to grow memory one sample at a time.
    """
    state = ProtobufObserveState()

    for sample in (19.0, 20.0, 21.0):
        decode_structure_updates(
            _stream_body(
                _get_property(
                    "DEVICE_18B430",
                    TEMPERATURE_TYPE_URL,
                    _field_bytes(1, _field_bytes(1, _field_float(1, sample))),
                    trait_label="current_temperature",
                )
            ),
            state,
        )

    assert len(state.pending_device_traits["18B430"]) == 1

    state.legacy_structure_ids["STRUCTURE_new"] = "legacy"
    updates = decode_structure_updates(
        _stream_body(
            _get_property(
                "STRUCTURE_new",
                PEER_DEVICES_TYPE_URL,
                _peer_devices(("DEVICE_18B430", NEST_KRYPTONITE_RESOURCE)),
            )
        ),
        state,
    )

    # Only the newest sample survives, and it is the one replayed.
    assert updates[-1].value == {"current_temperature": 21.0}


def test_pending_device_traits_are_capped():
    """Test the parked-trait buffer is bounded, oldest evicted first."""
    state = ProtobufObserveState()

    for index in range(MAX_PENDING_DEVICES + 5):
        decode_structure_updates(
            _stream_body(
                _get_property(
                    f"DEVICE_{index:06X}",
                    TEMPERATURE_TYPE_URL,
                    _field_bytes(1, _field_bytes(1, _field_float(1, 19.0))),
                )
            ),
            state,
        )

    assert len(state.pending_device_traits) == MAX_PENDING_DEVICES
    assert f"{0:06X}" not in state.pending_device_traits
    assert f"{MAX_PENDING_DEVICES + 4:06X}" in state.pending_device_traits


def test_decode_reemits_structures_when_the_user_arrives_late():
    """Test a late UserInfoTrait backfills the user id on known structures.

    Home/Away cannot build a command without it, and structure traits are not
    republished on any useful schedule.
    """
    state = ProtobufObserveState()
    decode_structure_updates(
        _stream_body(
            _get_property(
                "STRUCTURE_new",
                STRUCTURE_INFO_TYPE_URL,
                _field_string(1, "structure.legacy"),
            )
        ),
        state,
    )

    updates = decode_structure_updates(
        _stream_body(
            _get_property("USER_123", USER_INFO_TYPE_URL, _field_string(1, "user.leg"))
        ),
        state,
    )

    assert len(updates) == 1
    assert updates[0].resource_id == "STRUCTURE_new"
    assert updates[0].legacy_structure_id == "legacy"
    assert updates[0].user_id == "USER_123"

    # The same user id arriving again is not worth re-emitting for.
    assert (
        decode_structure_updates(
            _stream_body(
                _get_property(
                    "USER_123", USER_INFO_TYPE_URL, _field_string(1, "user.leg")
                )
            ),
            state,
        )
        == []
    )


def _peer_devices(*devices: tuple[str, str], firmware: str | None = None) -> bytes:
    """Build a PeerDevicesTrait payload listing `(resource_id, device_type)`."""
    entries = b""
    for resource_id, device_type in devices:
        data = _field_bytes(1, _field_string(1, resource_id)) + _field_bytes(
            2, _field_string(1, device_type)
        )
        if firmware is not None:
            data += _field_string(5, firmware)
        entries += _field_bytes(1, _field_bytes(2, data))
    return entries


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


def _located_settings(
    *,
    annotation_rid: str | None = None,
    label: str | None = None,
    legacy_uuid: str | None = None,
    fixture_type: int | None = None,
) -> bytes:
    """Build a DeviceLocatedSettingsTrait payload.

    Field numbers mirror protobuf_gen/nest/trait/located_pb2: whereAnnotationRid
    is a ResourceId (2), whereLabel a StringRef (5) and whereLegacyUuid a plain
    string (11).
    """
    payload = b""
    if annotation_rid is not None:
        payload += _field_bytes(2, _field_string(1, annotation_rid))
    if fixture_type is not None:
        payload += _field_bytes(4, _field_varint(1, fixture_type))
    if label is not None:
        payload += _field_bytes(5, _field_string(1, label))
    if legacy_uuid is not None:
        payload += _field_string(11, legacy_uuid)
    return payload
