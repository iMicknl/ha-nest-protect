"""Small protobuf helpers for Nest gateway commands and observe messages."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from struct import unpack
from uuid import uuid4

NEST_KRYPTONITE_RESOURCE = "nest.resource.NestKryptoniteResource"

STRUCTURE_MODE_HOME = 1
STRUCTURE_MODE_AWAY = 2
STRUCTURE_MODE_SLEEP = 3
STRUCTURE_MODE_VACATION = 4
STRUCTURE_MODE_REASON_EXPLICIT_INTENT = 1

USER_INFO_TYPE_URL = "type.nestlabs.com/nest.trait.user.UserInfoTrait"
STRUCTURE_INFO_TYPE_URL = "type.nestlabs.com/nest.trait.structure.StructureInfoTrait"
STRUCTURE_MODE_TYPE_URL = (
    "type.nestlabs.com/nest.trait.occupancy.StructureModeTrait"
)
PEER_DEVICES_TYPE_URL = "type.nestlabs.com/weave.trait.peerdevices.PeerDevicesTrait"
LIVENESS_TYPE_URL = "type.nestlabs.com/weave.trait.heartbeat.LivenessTrait"
DEVICE_IDENTITY_TYPE_URL = (
    "type.nestlabs.com/weave.trait.description.DeviceIdentityTrait"
)
DEVICE_LOCATED_SETTINGS_TYPE_URL = (
    "type.nestlabs.com/nest.trait.located.DeviceLocatedSettingsTrait"
)
TEMPERATURE_TYPE_URL = "type.nestlabs.com/nest.trait.sensor.TemperatureTrait"
BATTERY_TYPE_URL = "type.nestlabs.com/weave.trait.power.BatteryPowerSourceTrait"
STRUCTURE_MODE_CHANGE_TYPE_URL = (
    "type.nestlabs.com/"
    "nest.trait.occupancy.StructureModeTrait.StructureModeChangeRequest"
)
LIVENESS_DEVICE_STATUS_ONLINE = 1
VARINT_CONTINUATION_LIMIT = 0x7F
VARINT_CONTINUATION_BIT = 0x80
FIXED32_LENGTH = 4
FIXED64_LENGTH = 8
WIRE_TYPE_VARINT = 0
WIRE_TYPE_FIXED64 = 1
WIRE_TYPE_LENGTH_DELIMITED = 2
WIRE_TYPE_FIXED32 = 5
OBJECT_FIELD = 1
DYNAMIC_PROPERTY_FIELD = 3

OBSERVE_TRAITS = (
    "nest.trait.user.UserInfoTrait",
    "nest.trait.structure.StructureInfoTrait",
    "nest.trait.occupancy.StructureModeTrait",
    "weave.trait.peerdevices.PeerDevicesTrait",
    "weave.trait.heartbeat.LivenessTrait",
    "weave.trait.description.DeviceIdentityTrait",
    "nest.trait.located.DeviceLocatedSettingsTrait",
    "nest.trait.sensor.TemperatureTrait",
    "weave.trait.power.BatteryPowerSourceTrait",
)


@dataclass(frozen=True)
class ProtobufStructureUpdate:
    """Structure metadata or mode update decoded from the observe stream."""

    resource_id: str
    legacy_structure_id: str | None = None
    user_id: str | None = None
    away: bool | None = None


@dataclass(frozen=True)
class ProtobufDeviceUpdate:
    """Device update decoded from the observe stream."""

    object_key: str
    value: dict


ProtobufObserveUpdate = ProtobufStructureUpdate | ProtobufDeviceUpdate


@dataclass
class ProtobufObserveState:
    """Mutable mapping state needed while decoding protobuf observe messages."""

    user_id: str | None = None
    legacy_structure_ids: dict[str, str] = field(default_factory=dict)
    device_types: dict[str, str] = field(default_factory=dict)


def _varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    encoded = bytearray()
    while value > VARINT_CONTINUATION_LIMIT:
        encoded.append((value & VARINT_CONTINUATION_LIMIT) | VARINT_CONTINUATION_BIT)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _read_varint(buffer: bytes, offset: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    index = offset
    while index < len(buffer):
        byte = buffer[index]
        index += 1
        value |= (byte & VARINT_CONTINUATION_LIMIT) << shift
        if not byte & VARINT_CONTINUATION_BIT:
            return value, index
        shift += 7
    raise ValueError("Incomplete protobuf varint")


def _key(field_number: int, wire_type: int) -> bytes:
    return _varint((field_number << 3) | wire_type)


def _field_varint(field_number: int, value: int) -> bytes:
    return _key(field_number, 0) + _varint(value)


def _field_bytes(field_number: int, value: bytes) -> bytes:
    return _key(field_number, 2) + _varint(len(value)) + value


def _field_string(field_number: int, value: str) -> bytes:
    return _field_bytes(field_number, value.encode())


def _resource_id(resource_id: str) -> bytes:
    return _field_string(1, resource_id)


def encode_structure_mode_change_request(*, home: bool, user_id: str) -> bytes:
    """Encode StructureModeTrait.StructureModeChangeRequest."""
    return b"".join(
        (
            _field_varint(
                1,
                STRUCTURE_MODE_HOME if home else STRUCTURE_MODE_AWAY,
            ),
            _field_varint(2, STRUCTURE_MODE_REASON_EXPLICIT_INTENT),
            _field_bytes(3, _resource_id(user_id)),
        )
    )


def _any(type_url: str, value: bytes) -> bytes:
    return _field_string(1, type_url) + _field_bytes(2, value)


def _resource_request(resource_id: str) -> bytes:
    return _field_string(1, resource_id) + _field_string(2, str(uuid4()))


def _resource_command(*, home: bool, user_id: str) -> bytes:
    command = encode_structure_mode_change_request(home=home, user_id=user_id)
    return _field_string(1, "structure_mode") + _field_bytes(
        2, _any(STRUCTURE_MODE_CHANGE_TYPE_URL, command)
    )


def encode_structure_mode_resource_command_request(
    *,
    structure_resource_id: str,
    home: bool,
    user_id: str,
) -> bytes:
    """Encode nestlabs.gateway.v1.ResourceCommandRequest."""
    return b"".join(
        (
            _field_bytes(1, _resource_request(structure_resource_id)),
            _field_bytes(2, _resource_command(home=home, user_id=user_id)),
        )
    )


def encode_observe_request(traits: Iterable[str] = OBSERVE_TRAITS) -> bytes:
    """Encode the Nest GatewayService/Observe request trait list.

    Homebridge sends a much larger trait catalog; Home/Away only needs these
    structure/user traits.
    """
    return b"".join(
        (
            _field_bytes(1, b"\x02\x01"),
            *(_field_bytes(3, _field_string(1, trait)) for trait in traits),
        )
    )


def decode_observe_stream_frames(
    buffer: bytes,
) -> tuple[list[bytes], bytes]:
    """Decode complete StreamBody messages from the observe byte stream."""
    frames: list[bytes] = []
    offset = 0

    while offset < len(buffer):
        try:
            key, length_offset = _read_varint(buffer, offset)
            if key & 0x07 != WIRE_TYPE_LENGTH_DELIMITED:
                break
            frame_length, body_offset = _read_varint(buffer, length_offset)
        except ValueError:
            break

        frame_end = body_offset + frame_length
        if len(buffer) < frame_end:
            break

        frames.append(buffer[offset:frame_end])
        offset = frame_end

    return frames, buffer[offset:]


def decode_structure_updates(
    payload: bytes,
    state: ProtobufObserveState | None = None,
) -> list[ProtobufObserveUpdate]:
    """Decode structure and temperature sensor updates from a StreamBody."""
    state = state or ProtobufObserveState()
    updates: list[ProtobufObserveUpdate] = []

    for _, message in _bytes_fields(payload, 1):
        for _, get_property in _bytes_fields(message, 3):
            update = _decode_get_property(get_property, state)
            if isinstance(update, list):
                updates.extend(update)
            elif update:
                updates.append(update)

    return updates


def _decode_get_property(
    payload: bytes,
    state: ProtobufObserveState,
) -> ProtobufObserveUpdate | list[ProtobufObserveUpdate] | None:
    object_id = None
    type_url = None
    value = None

    for field_number, _, field_value in _decode_fields(payload):
        if field_number == OBJECT_FIELD and isinstance(field_value, bytes):
            object_id = _first_string_field(field_value, 1)
        elif field_number == DYNAMIC_PROPERTY_FIELD and isinstance(field_value, bytes):
            any_payload = _first_bytes_field(field_value, 1)
            if any_payload:
                type_url = _first_string_field(any_payload, 1)
                value = _first_bytes_field(any_payload, 2)

    if not object_id or not type_url or value is None:
        return None

    if type_url == USER_INFO_TYPE_URL:
        state.user_id = object_id
        return None

    if type_url == STRUCTURE_INFO_TYPE_URL:
        legacy_id = _first_string_field(value, 1)
        legacy_structure_id = legacy_id.split(".", 1)[1] if legacy_id else None
        if legacy_structure_id:
            state.legacy_structure_ids[object_id] = legacy_structure_id
        return ProtobufStructureUpdate(
            resource_id=object_id,
            legacy_structure_id=legacy_structure_id,
            user_id=state.user_id,
        )

    if type_url == STRUCTURE_MODE_TYPE_URL:
        structure_mode = _first_varint_field(value, 1)
        if structure_mode is None:
            return None

        return ProtobufStructureUpdate(
            resource_id=object_id,
            legacy_structure_id=state.legacy_structure_ids.get(object_id),
            user_id=state.user_id,
            away=structure_mode
            in {STRUCTURE_MODE_AWAY, STRUCTURE_MODE_SLEEP, STRUCTURE_MODE_VACATION},
        )

    if type_url == PEER_DEVICES_TYPE_URL:
        return _decode_peer_devices(object_id, value, state)

    return _decode_device_property(object_id, type_url, value, state)


def _decode_peer_devices(
    structure_resource_id: str,
    payload: bytes,
    state: ProtobufObserveState,
) -> list[ProtobufDeviceUpdate]:
    legacy_structure_id = state.legacy_structure_ids.get(structure_resource_id)
    if not legacy_structure_id:
        return []

    updates: list[ProtobufDeviceUpdate] = []
    for _, peer_device in _bytes_fields(payload, 1):
        data = _first_bytes_field(peer_device, 2)
        if not data:
            continue

        resource_id = _indirect_string(data, 1)
        device_type = _indirect_string(data, 2)
        if not resource_id or device_type != NEST_KRYPTONITE_RESOURCE:
            continue

        device_id = _legacy_device_id(resource_id)
        state.device_types[device_id] = device_type
        updates.append(
            ProtobufDeviceUpdate(
                object_key=f"kryptonite.{device_id}",
                value={
                    "using_protobuf": True,
                    "device_id": device_id,
                    "structure_id": legacy_structure_id,
                    "current_version": _first_string_field(data, 5),
                    "user_id": state.user_id,
                    "protobuf_device_type": device_type,
                },
            )
        )

    return updates


def _decode_device_property(
    resource_id: str,
    type_url: str,
    payload: bytes,
    state: ProtobufObserveState,
) -> ProtobufDeviceUpdate | None:
    device_id = _legacy_device_id(resource_id)
    if state.device_types.get(device_id) != NEST_KRYPTONITE_RESOURCE:
        return None

    if type_url == LIVENESS_TYPE_URL:
        return _decode_liveness(device_id, payload)

    if type_url == DEVICE_IDENTITY_TYPE_URL:
        return _device_update(
            device_id,
            {
                "model": _indirect_string(payload, 4),
                "serial_number": _first_string_field(payload, 6),
                "current_version": _first_string_field(payload, 7),
            },
        )

    if type_url == DEVICE_LOCATED_SETTINGS_TYPE_URL:
        update = {"where_id": _indirect_string(payload, 2)}
        fixture_type = _first_bytes_field(payload, 4)
        if fixture_type:
            update["fixture_type"] = _first_varint_field(fixture_type, 1)
        return _device_update(device_id, update)

    if type_url == TEMPERATURE_TYPE_URL:
        temperature = _nested_float(payload, 1, 1, 1)
        if temperature is None:
            return None
        return ProtobufDeviceUpdate(
            object_key=f"kryptonite.{device_id}",
            value={"current_temperature": temperature},
        )

    if type_url == BATTERY_TYPE_URL:
        return _device_update(
            device_id,
            {
                "battery_status": _first_varint_field(payload, 32),
                "battery_level": _nested_float(payload, 33, 1, 1),
            },
        )

    return None


def _decode_liveness(device_id: str, payload: bytes) -> ProtobufDeviceUpdate | None:
    status = _first_varint_field(payload, 1)
    if status is None:
        return None
    return ProtobufDeviceUpdate(
        object_key=f"kryptonite.{device_id}",
        value={"is_online": status == LIVENESS_DEVICE_STATUS_ONLINE},
    )


def _device_update(device_id: str, values: dict) -> ProtobufDeviceUpdate | None:
    value = {key: val for key, val in values.items() if val is not None}
    if not value:
        return None
    return ProtobufDeviceUpdate(object_key=f"kryptonite.{device_id}", value=value)


def _decode_fields(payload: bytes) -> list[tuple[int, int, int | bytes]]:
    fields: list[tuple[int, int, int | bytes]] = []
    offset = 0

    while offset < len(payload):
        key, offset = _read_varint(payload, offset)
        field_number = key >> 3
        wire_type = key & 0x07

        if wire_type == WIRE_TYPE_VARINT:
            value, offset = _read_varint(payload, offset)
        elif wire_type == WIRE_TYPE_LENGTH_DELIMITED:
            length, offset = _read_varint(payload, offset)
            value = payload[offset : offset + length]
            offset += length
        elif wire_type == WIRE_TYPE_FIXED32:
            value = payload[offset : offset + FIXED32_LENGTH]
            offset += FIXED32_LENGTH
        elif wire_type == WIRE_TYPE_FIXED64:
            value = payload[offset : offset + FIXED64_LENGTH]
            offset += FIXED64_LENGTH
        else:
            break

        fields.append((field_number, wire_type, value))

    return fields


def _bytes_fields(payload: bytes, field_number: int) -> list[tuple[int, bytes]]:
    return [
        (decoded_field, value)
        for decoded_field, wire_type, value in _decode_fields(payload)
        if decoded_field == field_number
        and wire_type == WIRE_TYPE_LENGTH_DELIMITED
        and isinstance(value, bytes)
    ]


def _first_bytes_field(payload: bytes, field_number: int) -> bytes | None:
    for decoded_field, wire_type, value in _decode_fields(payload):
        if (
            decoded_field == field_number
            and wire_type == WIRE_TYPE_LENGTH_DELIMITED
            and isinstance(value, bytes)
        ):
            return value
    return None


def _first_string_field(payload: bytes, field_number: int) -> str | None:
    value = _first_bytes_field(payload, field_number)
    if value is None:
        return None
    return value.decode()


def _first_varint_field(payload: bytes, field_number: int) -> int | None:
    for decoded_field, wire_type, value in _decode_fields(payload):
        if (
            decoded_field == field_number
            and wire_type == WIRE_TYPE_VARINT
            and isinstance(value, int)
        ):
            return value
    return None


def _first_fixed32_field(payload: bytes, field_number: int) -> bytes | None:
    for decoded_field, wire_type, value in _decode_fields(payload):
        if (
            decoded_field == field_number
            and wire_type == WIRE_TYPE_FIXED32
            and isinstance(value, bytes)
        ):
            return value
    return None


def _first_float_field(payload: bytes, field_number: int) -> float | None:
    value = _first_fixed32_field(payload, field_number)
    if value is None:
        return None
    return unpack("<f", value)[0]


def _nested_float(payload: bytes, *field_numbers: int) -> float | None:
    current = payload
    for field_number in field_numbers[:-1]:
        current = _first_bytes_field(current, field_number)
        if current is None:
            return None
    return _first_float_field(current, field_numbers[-1])


def _indirect_string(payload: bytes, field_number: int) -> str | None:
    indirect = _first_bytes_field(payload, field_number)
    if not indirect:
        return None
    return _first_string_field(indirect, 1)


def _legacy_device_id(resource_id: str) -> str:
    return resource_id.removeprefix("DEVICE_")
