"""Small protobuf helpers for Nest gateway commands and observe messages."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from struct import unpack
from uuid import uuid4

_LOGGER = logging.getLogger(__package__)

NEST_KRYPTONITE_RESOURCE = "nest.resource.NestKryptoniteResource"

# Thermostat resource types, mirroring homebridge-nest's `peer_devices` handling
# and tronikos/nest_legacy's model table. Google-branded resources are the newer
# hardware (Thermostat 2020, Learning Thermostat 4th gen).
NEST_THERMOSTAT_RESOURCES = frozenset(
    {
        "nest.resource.NestLearningThermostat1Resource",
        "nest.resource.NestLearningThermostat2Resource",
        "nest.resource.NestLearningThermostat3Resource",
        "nest.resource.NestLearningThermostat3v2Resource",
        "nest.resource.NestThermostat3Resource",
        "nest.resource.NestAmber1DisplayResource",
        "nest.resource.NestAmber2DisplayResource",
        "nest.resource.NestAgateDisplayResource",  # Thermostat E
        "nest.resource.NestOnyxResource",  # Thermostat E (US, 1st gen)
        "google.resource.GoogleZirconium1Resource",  # Thermostat (2020)
        "google.resource.GoogleBismuth1Resource",  # Learning Thermostat (4th gen)
    }
)

# Protobuf resource type -> legacy bucket prefix. Thermostats use the same
# `device.` prefix the legacy REST API gives them.
BUCKET_PREFIXES = {NEST_KRYPTONITE_RESOURCE: "kryptonite"} | dict.fromkeys(
    NEST_THERMOSTAT_RESOURCES, "device"
)

STRUCTURE_MODE_HOME = 1
STRUCTURE_MODE_AWAY = 2
STRUCTURE_MODE_SLEEP = 3
STRUCTURE_MODE_VACATION = 4
STRUCTURE_MODE_REASON_EXPLICIT_INTENT = 1

USER_INFO_TYPE_URL = "type.nestlabs.com/nest.trait.user.UserInfoTrait"
STRUCTURE_INFO_TYPE_URL = "type.nestlabs.com/nest.trait.structure.StructureInfoTrait"
STRUCTURE_MODE_TYPE_URL = "type.nestlabs.com/nest.trait.occupancy.StructureModeTrait"
PEER_DEVICES_TYPE_URL = "type.nestlabs.com/weave.trait.peerdevices.PeerDevicesTrait"
LIVENESS_TYPE_URL = "type.nestlabs.com/weave.trait.heartbeat.LivenessTrait"
DEVICE_IDENTITY_TYPE_URL = (
    "type.nestlabs.com/weave.trait.description.DeviceIdentityTrait"
)
DEVICE_LOCATED_SETTINGS_TYPE_URL = (
    "type.nestlabs.com/nest.trait.located.DeviceLocatedSettingsTrait"
)
TEMPERATURE_TYPE_URL = "type.nestlabs.com/nest.trait.sensor.TemperatureTrait"
HUMIDITY_TYPE_URL = "type.nestlabs.com/nest.trait.sensor.HumidityTrait"
BATTERY_TYPE_URL = "type.nestlabs.com/weave.trait.power.BatteryPowerSourceTrait"
RCS_SETTINGS_TYPE_URL = (
    "type.nestlabs.com/nest.trait.hvac.RemoteComfortSensingSettingsTrait"
)
STRUCTURE_MODE_CHANGE_TYPE_URL = (
    "type.nestlabs.com/"
    "nest.trait.occupancy.StructureModeTrait.StructureModeChangeRequest"
)
LIVENESS_DEVICE_STATUS_ONLINE = 1

# DeviceLocatedSettingsTrait fields, per protobuf_gen/nest/trait/located_pb2.
# `whereAnnotationRid` is a protobuf annotation resource id and is *not*
# interchangeable with the legacy where uuid the `where.` REST buckets are keyed
# by — that one lives in `whereLegacyUuid`.
WHERE_ANNOTATION_RID_FIELD = 2
FIXTURE_TYPE_FIELD = 4
WHERE_LABEL_FIELD = 5
WHERE_LEGACY_UUID_FIELD = 11

# RemoteComfortSensingSettingsTrait.RcsSourceType — which temperature source the
# thermostat is currently controlling on.
RCS_SOURCE_TYPE_BACKPLATE = 1
RCS_SOURCE_TYPE_SINGLE_SENSOR = 2
RCS_SOURCE_TYPE_MULTI_SENSOR = 3

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
# ObjectIdPair is {id = 1, key = 2}, where `key` is the trait label.
OBJECT_ID_FIELD = 1
TRAIT_LABEL_FIELD = 2
# TraitState.stateTypes — whether a patch carries the confirmed (device-reported)
# or accepted (server-side) copy of the trait. Decoded for logging only: the
# observe request asks for both, so telling them apart matters when diagnosing
# updates that appear to stall.
STATE_TYPES_FIELD = 2
STATE_TYPE_NAMES = {1: "confirmed", 2: "accepted"}

TRAIT_LABEL_BACKPLATE_TEMPERATURE = "backplate_temperature"
TRAIT_LABEL_CURRENT_TEMPERATURE = "current_temperature"
TRAIT_LABEL_BACKPLATE_HUMIDITY = "backplate_humidity"
TRAIT_LABEL_CURRENT_HUMIDITY = "current_humidity"

# Thermostats publish TemperatureTrait and HumidityTrait twice, and only the
# trait label tells the two instances apart: `backplate_*` is the device's own
# sensor, `current_*` the effective reading, which follows the selected remote
# comfort sensor. The label doubles as the bucket key. Kryptonite sensors send a
# single unlabelled instance, which stays the current reading.
TEMPERATURE_TRAIT_LABELS = frozenset(
    {TRAIT_LABEL_BACKPLATE_TEMPERATURE, TRAIT_LABEL_CURRENT_TEMPERATURE}
)
HUMIDITY_TRAIT_LABELS = frozenset(
    {TRAIT_LABEL_BACKPLATE_HUMIDITY, TRAIT_LABEL_CURRENT_HUMIDITY}
)
# Bucketized labels carry temperature history series, not the current reading.
BUCKETIZED_LABEL_SUFFIX = "_bucketized"

# Caps on the parked-trait buffers. The observe stream is meant to run for
# weeks, so anything keyed by a resource id the gateway never goes on to
# explain has to be forgettable. Within one device the parked set is keyed by
# trait, so a device republishing its temperature every minute replaces its own
# entry instead of adding another; these caps bound the number of *resources*
# that can be waiting at once. Oldest-first eviction.
MAX_PENDING_DEVICES = 64
MAX_PENDING_STRUCTURES = 8

OBSERVE_TRAITS = (
    "nest.trait.user.UserInfoTrait",
    "nest.trait.structure.StructureInfoTrait",
    "nest.trait.occupancy.StructureModeTrait",
    "weave.trait.peerdevices.PeerDevicesTrait",
    "weave.trait.heartbeat.LivenessTrait",
    "weave.trait.description.DeviceIdentityTrait",
    "nest.trait.located.DeviceLocatedSettingsTrait",
    "nest.trait.sensor.TemperatureTrait",
    "nest.trait.sensor.HumidityTrait",
    "weave.trait.power.BatteryPowerSourceTrait",
    "nest.trait.hvac.RemoteComfortSensingSettingsTrait",
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
class _TraitState:
    """One decoded `nestlabs.gateway.v2.TraitState` off the observe stream."""

    object_id: str | None = None
    trait_label: str | None = None
    type_url: str | None = None
    value: bytes | None = None
    state_types: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class _PendingTrait:
    """A device trait held back until its device type is known."""

    type_url: str
    value: bytes
    trait_label: str | None
    state_types: tuple[int, ...]


@dataclass
class ProtobufObserveState:
    """Mutable mapping state needed while decoding protobuf observe messages.

    Owned by the caller and reused across observe streams: the gateway
    re-enumerates from scratch on every reconnect, and re-learning the device
    map each time would re-run the ordering race below on every reconnect.
    """

    user_id: str | None = None
    legacy_structure_ids: dict[str, str] = field(default_factory=dict)
    device_types: dict[str, str] = field(default_factory=dict)
    # Devices the gateway has named whose resource type this integration does
    # not model. Their traits are dropped on sight rather than parked, so a
    # Protect or Guard publishing every few seconds can't churn the buffers.
    unsupported_devices: set[str] = field(default_factory=set)
    # device_id -> {(type_url, trait_label): trait}. Traits that arrived before
    # PeerDevicesTrait revealed the device they belong to.
    pending_device_traits: dict[str, dict[tuple[str, str | None], _PendingTrait]] = (
        field(default_factory=dict)
    )
    # structure_resource_id -> latest PeerDevicesTrait payload seen before
    # StructureInfoTrait gave the structure a legacy id.
    pending_peer_devices: dict[str, bytes] = field(default_factory=dict)


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
    """Decode complete StreamBody messages from the observe byte stream.

    Returns the frames that were complete plus whatever bytes are left over,
    which the caller prepends to the next chunk. Every StreamBody field the
    gateway sends is length-delimited, so any other wire type means the buffer
    has lost sync — keeping those bytes around would poison every later chunk
    and silently wedge the stream, so they are dropped instead.
    """
    frames: list[bytes] = []
    offset = 0

    while offset < len(buffer):
        try:
            key, length_offset = _read_varint(buffer, offset)
        except ValueError:
            # Varint split across chunks; wait for more bytes.
            break

        if key & 0x07 != WIRE_TYPE_LENGTH_DELIMITED:
            _LOGGER.warning(
                "Observe stream out of sync at offset %d (wire type %d), "
                "discarding %d buffered bytes",
                offset,
                key & 0x07,
                len(buffer) - offset,
            )
            return frames, b""

        try:
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
    trait = _scan_trait_state(payload)
    object_id = trait.object_id
    type_url = trait.type_url
    value = trait.value

    if not object_id or not type_url or value is None:
        return None

    if trait.trait_label and trait.trait_label.endswith(BUCKETIZED_LABEL_SUFFIX):
        return None

    if type_url == USER_INFO_TYPE_URL:
        if state.user_id == object_id:
            return None
        state.user_id = object_id
        # Structures decoded before the user was known carry `user_id=None`,
        # which leaves the Home/Away switch unable to build a command. Re-emit
        # them now rather than waiting for the structure to publish again.
        return [
            ProtobufStructureUpdate(
                resource_id=resource_id,
                legacy_structure_id=legacy_structure_id,
                user_id=object_id,
            )
            for resource_id, legacy_structure_id in state.legacy_structure_ids.items()
        ]

    if type_url == STRUCTURE_INFO_TYPE_URL:
        legacy_id = _first_string_field(value, 1)
        legacy_structure_id = legacy_id.split(".", 1)[1] if legacy_id else None
        updates: list[ProtobufObserveUpdate] = [
            ProtobufStructureUpdate(
                resource_id=object_id,
                legacy_structure_id=legacy_structure_id,
                user_id=state.user_id,
            )
        ]
        if legacy_structure_id:
            state.legacy_structure_ids[object_id] = legacy_structure_id
            # A PeerDevicesTrait that arrived first can now be resolved, which
            # in turn releases every device trait parked behind it.
            updates.extend(_drain_pending_peer_devices(object_id, state))
        return updates

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

    return _decode_device_property(
        object_id,
        type_url,
        value,
        state,
        trait_label=trait.trait_label,
        state_types=trait.state_types,
    )


def _scan_trait_state(payload: bytes) -> _TraitState:
    """Pull the identity, state types and Any-wrapped value out of a TraitState."""
    trait = _TraitState()

    for field_number, wire_type, field_value in _decode_fields(payload):
        if field_number == OBJECT_FIELD and isinstance(field_value, bytes):
            trait.object_id = _first_string_field(field_value, OBJECT_ID_FIELD)
            trait.trait_label = _first_string_field(field_value, TRAIT_LABEL_FIELD)
        elif field_number == STATE_TYPES_FIELD:
            trait.state_types.extend(_state_types(wire_type, field_value))
        elif field_number == DYNAMIC_PROPERTY_FIELD and isinstance(field_value, bytes):
            any_payload = _first_bytes_field(field_value, 1)
            if any_payload:
                trait.type_url = _first_string_field(any_payload, 1)
                trait.value = _first_bytes_field(any_payload, 2)

    return trait


def _state_types(wire_type: int, field_value: int | bytes) -> list[int]:
    """Decode TraitState.stateTypes, packed or repeated."""
    if wire_type == WIRE_TYPE_VARINT and isinstance(field_value, int):
        return [field_value]

    if wire_type != WIRE_TYPE_LENGTH_DELIMITED or not isinstance(field_value, bytes):
        return []

    values: list[int] = []
    offset = 0
    while offset < len(field_value):
        try:
            value, offset = _read_varint(field_value, offset)
        except ValueError:
            break
        values.append(value)
    return values


def _decode_peer_devices(
    structure_resource_id: str,
    payload: bytes,
    state: ProtobufObserveState,
) -> list[ProtobufObserveUpdate]:
    legacy_structure_id = state.legacy_structure_ids.get(structure_resource_id)
    if not legacy_structure_id:
        # StructureInfoTrait hasn't landed yet. Dropping the list here would
        # leave every device on this structure unmapped for the life of the
        # stream, so hold it until the structure is named.
        _park_peer_devices(structure_resource_id, payload, state)
        return []

    updates: list[ProtobufObserveUpdate] = []
    resolved: list[str] = []
    for _, peer_device in _bytes_fields(payload, 1):
        data = _first_bytes_field(peer_device, 2)
        if not data:
            continue

        resource_id = _indirect_string(data, 1)
        device_type = _indirect_string(data, 2)
        if not resource_id or not device_type:
            continue

        device_id = _legacy_device_id(resource_id)

        prefix = BUCKET_PREFIXES.get(device_type)
        if not prefix:
            # Now known to be a type we don't model, so stop holding — and stop
            # accepting — traits for it.
            if device_id not in state.unsupported_devices:
                _LOGGER.debug(
                    "Observe: device %s has unsupported resource type %s, "
                    "ignoring its traits",
                    device_id,
                    device_type,
                )
            state.unsupported_devices.add(device_id)
            state.pending_device_traits.pop(device_id, None)
            continue

        state.device_types[device_id] = device_type
        resolved.append(device_id)
        # `current_version` and `user_id` are absent when the gateway omits the
        # firmware version, or when this trait wins the race against
        # UserInfoTrait. Writing None would clobber a good value on the bucket.
        value = {
            "using_protobuf": True,
            "device_id": device_id,
            "structure_id": legacy_structure_id,
            "protobuf_device_type": device_type,
        } | {
            key: optional
            for key, optional in (
                ("current_version", _first_string_field(data, 5)),
                ("user_id", state.user_id),
            )
            if optional is not None
        }

        updates.append(
            ProtobufDeviceUpdate(object_key=f"{prefix}.{device_id}", value=value)
        )

    for device_id in resolved:
        updates.extend(_drain_pending_device_traits(device_id, state))

    return updates


def _park_peer_devices(
    structure_resource_id: str, payload: bytes, state: ProtobufObserveState
) -> None:
    """Hold a PeerDevicesTrait until its structure has a legacy id."""
    pending = state.pending_peer_devices
    if structure_resource_id not in pending:
        _evict_oldest(pending, MAX_PENDING_STRUCTURES, "peer devices")
    pending[structure_resource_id] = payload
    _LOGGER.debug(
        "Observe: holding peer devices for unmapped structure %s",
        structure_resource_id,
    )


def _drain_pending_peer_devices(
    structure_resource_id: str, state: ProtobufObserveState
) -> list[ProtobufObserveUpdate]:
    """Decode a PeerDevicesTrait held back for this structure, if any."""
    payload = state.pending_peer_devices.pop(structure_resource_id, None)
    if payload is None:
        return []

    _LOGGER.debug(
        "Observe: replaying held peer devices for structure %s",
        structure_resource_id,
    )
    return _decode_peer_devices(structure_resource_id, payload, state)


def _park_device_trait(
    state: ProtobufObserveState, device_id: str, pending: _PendingTrait
) -> None:
    """Hold a device trait until PeerDevicesTrait reveals the device's type.

    Keyed by trait, so repeated publishes of the same trait supersede each
    other rather than accumulating — a device can sit unmapped for the life of
    the stream without the buffer growing.
    """
    parked = state.pending_device_traits.get(device_id)
    if parked is None:
        _evict_oldest(state.pending_device_traits, MAX_PENDING_DEVICES, "device traits")
        parked = state.pending_device_traits.setdefault(device_id, {})

    parked[(pending.type_url, pending.trait_label)] = pending
    _LOGGER.debug(
        "Observe: holding trait %s for unmapped device %s", pending.type_url, device_id
    )


def _drain_pending_device_traits(
    device_id: str, state: ProtobufObserveState
) -> list[ProtobufObserveUpdate]:
    """Decode every trait held back for a device that is now mapped."""
    parked = state.pending_device_traits.pop(device_id, None)
    if not parked:
        return []

    _LOGGER.debug(
        "Observe: replaying %d held trait(s) for device %s", len(parked), device_id
    )
    updates: list[ProtobufObserveUpdate] = []
    for pending in parked.values():
        update = _decode_device_property(
            device_id,
            pending.type_url,
            pending.value,
            state,
            trait_label=pending.trait_label,
            state_types=list(pending.state_types),
        )
        if update:
            updates.append(update)
    return updates


def _evict_oldest(buffer: dict, limit: int, what: str) -> None:
    """Make room for one more entry in a parking buffer, oldest first."""
    while len(buffer) >= limit:
        evicted, _ = next(iter(buffer.items()))
        del buffer[evicted]
        _LOGGER.debug(
            "Observe: %s buffer full, dropping held entry for %s", what, evicted
        )


def _decode_device_property(
    resource_id: str,
    type_url: str,
    payload: bytes,
    state: ProtobufObserveState,
    *,
    trait_label: str | None = None,
    state_types: list[int] | None = None,
) -> ProtobufDeviceUpdate | None:
    device_id = _legacy_device_id(resource_id)
    device_type = state.device_types.get(device_id)
    prefix = BUCKET_PREFIXES.get(device_type) if device_type else None
    if not prefix:
        # The gateway gives no ordering guarantee between PeerDevicesTrait —
        # which is the only thing that maps a device id to a type — and the
        # per-device traits. Discarding here used to lose the reading until the
        # device next republished it, which for temperature is tens of minutes
        # and for RCS settings can be hours.
        if device_id not in state.unsupported_devices:
            _park_device_trait(
                state,
                device_id,
                _PendingTrait(
                    type_url=type_url,
                    value=payload,
                    trait_label=trait_label,
                    state_types=tuple(state_types or ()),
                ),
            )
        return None

    object_key = f"{prefix}.{device_id}"

    if type_url == LIVENESS_TYPE_URL:
        return _decode_liveness(object_key, payload)

    if type_url == DEVICE_IDENTITY_TYPE_URL:
        return _device_update(
            object_key,
            {
                "model": _indirect_string(payload, 4),
                "serial_number": _first_string_field(payload, 6),
                "current_version": _first_string_field(payload, 7),
            },
        )

    if type_url == DEVICE_LOCATED_SETTINGS_TYPE_URL:
        update = {
            # Only `whereLegacyUuid` matches the keys `where.` REST buckets use,
            # so it is the one that may be written to `where_id`. The annotation
            # rid is kept under its own key for diagnostics.
            "where_id": _first_string_field(payload, WHERE_LEGACY_UUID_FIELD),
            "where_label": _indirect_string(payload, WHERE_LABEL_FIELD),
            "where_annotation_rid": _indirect_string(
                payload, WHERE_ANNOTATION_RID_FIELD
            ),
        }
        fixture_type = _first_bytes_field(payload, FIXTURE_TYPE_FIELD)
        if fixture_type:
            update["fixture_type"] = _first_varint_field(fixture_type, 1)
        return _device_update(object_key, update)

    if type_url == TEMPERATURE_TYPE_URL:
        return _decode_sensor_sample(
            object_key,
            payload,
            trait_label=trait_label,
            state_types=state_types,
            known_labels=TEMPERATURE_TRAIT_LABELS,
            unlabelled_key=TRAIT_LABEL_CURRENT_TEMPERATURE,
        )

    if type_url == HUMIDITY_TYPE_URL:
        # HumidityTrait has the same nesting and the same paired labels.
        return _decode_sensor_sample(
            object_key,
            payload,
            trait_label=trait_label,
            state_types=state_types,
            known_labels=HUMIDITY_TRAIT_LABELS,
            unlabelled_key=TRAIT_LABEL_CURRENT_HUMIDITY,
        )

    if type_url == BATTERY_TYPE_URL:
        return _device_update(
            object_key,
            {
                "battery_status": _first_varint_field(payload, 32),
                "battery_level": _nested_float(payload, 33, 1, 1),
            },
        )

    if type_url == RCS_SETTINGS_TYPE_URL:
        # Not filtered through _device_update: `None` and `[]` have to survive so
        # that reverting to the thermostat's own sensor clears the old selection.
        return ProtobufDeviceUpdate(
            object_key=object_key, value=_decode_rcs_settings(payload)
        )

    _LOGGER.debug("Observe: ignoring trait %s on %s", type_url, object_key)
    return None


def _decode_sensor_sample(
    object_key: str,
    payload: bytes,
    *,
    trait_label: str | None,
    state_types: list[int] | None,
    known_labels: frozenset[str],
    unlabelled_key: str,
) -> ProtobufDeviceUpdate | None:
    """Decode a TemperatureTrait/HumidityTrait sample into its bucket key.

    An unrecognised label is dropped rather than folded into the current
    reading: a device publishing an instance this integration does not know
    about must not be able to overwrite the ambient value with it.
    """
    if trait_label and trait_label not in known_labels:
        _LOGGER.debug(
            "Observe: ignoring unknown sensor trait label %s on %s",
            trait_label,
            object_key,
        )
        return None

    key = trait_label or unlabelled_key

    sample = _nested_float(payload, 1, 1, 1)
    if sample is None:
        _LOGGER.debug("Observe: no sample in %s trait on %s", key, object_key)
        return None

    _LOGGER.debug(
        "Observe: %s %s=%s (%s)",
        object_key,
        key,
        sample,
        ", ".join(STATE_TYPE_NAMES.get(s, str(s)) for s in state_types or [])
        or "no state types",
    )
    return ProtobufDeviceUpdate(object_key=object_key, value={key: sample})


def _decode_rcs_settings(payload: bytes) -> dict:
    """Decode RemoteComfortSensingSettingsTrait.

    Reports which temperature source the thermostat controls on: its own
    backplate, one associated Nest Temperature Sensor, or an average over a
    group of them.
    """
    selection = _first_bytes_field(payload, 2)
    source_type = _first_varint_field(selection, 1) if selection else None
    single_sensor = _indirect_string(selection, 2) if selection else None

    multi_settings = _first_bytes_field(payload, 5)
    multi_group = (
        [
            resource_id
            for _, group in _bytes_fields(multi_settings, 2)
            if (resource_id := _first_string_field(group, 1))
        ]
        if multi_settings
        else []
    )

    if source_type == RCS_SOURCE_TYPE_MULTI_SENSOR:
        active = multi_group
    elif source_type == RCS_SOURCE_TYPE_SINGLE_SENSOR and single_sensor:
        active = [single_sensor]
    else:
        active = []

    associated = [
        resource_id
        for _, sensor in _bytes_fields(payload, 4)
        if (resource_id := _indirect_string(sensor, 1))
    ]

    return {
        "rcs_control_mode": _first_varint_field(payload, 1),
        "rcs_source_type": source_type,
        "active_rcs_sensors": [_kryptonite_key(r) for r in active],
        "associated_rcs_sensors": [_kryptonite_key(r) for r in associated],
    }


def _kryptonite_key(resource_id: str) -> str:
    """Map a sensor resource id to its legacy temperature sensor bucket key."""
    return f"kryptonite.{_legacy_device_id(resource_id)}"


def _decode_liveness(object_key: str, payload: bytes) -> ProtobufDeviceUpdate | None:
    status = _first_varint_field(payload, 1)
    if status is None:
        return None
    return ProtobufDeviceUpdate(
        object_key=object_key,
        value={"is_online": status == LIVENESS_DEVICE_STATUS_ONLINE},
    )


def _device_update(object_key: str, values: dict) -> ProtobufDeviceUpdate | None:
    value = {key: val for key, val in values.items() if val is not None}
    if not value:
        return None
    return ProtobufDeviceUpdate(object_key=object_key, value=value)


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
