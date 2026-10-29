"""gRPC-web client for the Nest gateway.

Talks to nestlabs.gateway.v2.GatewayService/Observe (streaming state) and
nestlabs.gateway.v1.ResourceApi/SendCommand (commands), both at
grpc-web.production.nest.com. Reuses the ha-nest-protect session's
Basic access_token verbatim — no separate JWT issuance is required.

This is a focused port of slices of tronikos/nest_legacy.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aiohttp import ClientTimeout, SocketTimeoutError

from .const import PROTOBUF_USER_AGENT
from .exceptions import NestLockAuthException, NestLockCommandException
from .lock_models import LockBoltState, LockState
from .protobuf_gen.google.rpc import streambody_pb2
from .protobuf_gen.nest.trait import hvac_pb2 as nest_hvac_pb2
from .protobuf_gen.nest.trait import located_pb2 as nest_located_pb2
from .protobuf_gen.nest.trait import occupancy_pb2 as nest_occupancy_pb2
from .protobuf_gen.nest.trait import sensor_pb2 as nest_sensor_pb2
from .protobuf_gen.nest.trait import structure_pb2 as nest_structure_pb2
from .protobuf_gen.nestlabs.gateway import v1_pb2, v2_pb2
from .protobuf_gen.weave.trait import description_pb2 as weave_description_pb2
from .protobuf_gen.weave.trait import power_pb2 as weave_power_pb2
from .protobuf_gen.weave.trait import security_pb2 as weave_security_pb2
from .thermostat_models import TemperatureSource, ThermostatState

if TYPE_CHECKING:
    from .client import NestClient

_LOGGER = logging.getLogger(__name__)

OBSERVE_ENDPOINT = "/nestlabs.gateway.v2.GatewayService/Observe"
SEND_COMMAND_ENDPOINT = "/nestlabs.gateway.v1.ResourceApi/SendCommand"

_NESTLABS_TYPE_URL_PREFIX = "type.nestlabs.com/"

# Seconds without data before the stream is treated as dead. The gateway sends
# nothing while the locks are quiet, so this also sets how often an idle stream
# is re-opened.
_SOCK_READ_TIMEOUT = 900
_CONNECT_TIMEOUT = 60
_SEND_COMMAND_TIMEOUT = 30
# gRPC status code the gateway uses to end an observe stream at its deadline.
_GRPC_DEADLINE_EXCEEDED = 4
_RECONNECT_INITIAL_DELAY = 1.0
_RECONNECT_MAX_DELAY = 60.0
# Every stream opens with the current state, so only one that stayed up this
# long proves the gateway is healthy enough to reset the reconnect backoff.
_STREAM_HEALTHY_SECONDS = 60.0

# How long the observe stream must have been running, cumulatively across
# reconnects, before an account is declared to have no lock or thermostat. The
# gateway sends its resource metas and the initial trait states in the first
# moments of a stream, but the ordering between the two isn't guaranteed, so a
# settle window keeps a slow initial dump from being mistaken for "nothing to
# watch here".
_NO_DEVICE_SETTLE_SECONDS = 30.0

# HTTP statuses that mean the session token was rejected rather than that the
# gateway had a transient problem. Retrying these forever never recovers.
_AUTH_ERROR_STATUSES = frozenset({401, 403})

# google.rpc.Code values. PERMISSION_DENIED usually means a genuine
# authorization refusal that a new session won't fix, so only its
# "authentication failed" variant is treated as a rejected session.
_GRPC_PERMISSION_DENIED = 7
_GRPC_UNAUTHENTICATED = 16

# Protobuf wire-type for length-delimited fields. Every StreamBody field the
# gateway sends is length-delimited (tag wire-type == 2); any other wire-type
# means the buffer is out of sync and must be reset.
_WIRE_TYPE_LENGTH_DELIMITED = 2

# The trait types we ask the server to stream. Limited set keeps bandwidth and
# parsing cost down vs subscribing to the entire trait surface.
# The three nest.trait.located traits are needed to resolve human-readable
# room labels: DeviceLocatedSettingsTrait lives on each lock, the two
# *Annotations traits live on the structure resource as a where_id → label
# catalogue. StructureInfoTrait links each protobuf structure to the REST
# `structure.<id>` bucket, which commands to that home need. The sensor and
# RemoteComfortSensingSettingsTrait traits carry thermostat readings.
_TRAIT_NAME_TO_CLASS: dict[str, type] = {
    cls.DESCRIPTOR.full_name: cls
    for cls in (
        weave_security_pb2.BoltLockTrait,
        weave_description_pb2.DeviceIdentityTrait,
        weave_description_pb2.LabelSettingsTrait,
        weave_power_pb2.BatteryPowerSourceTrait,
        nest_located_pb2.DeviceLocatedSettingsTrait,
        nest_located_pb2.LocatedAnnotationsTrait,
        nest_located_pb2.CustomLocatedAnnotationsTrait,
        nest_structure_pb2.StructureInfoTrait,
        nest_sensor_pb2.TemperatureTrait,
        nest_sensor_pb2.HumidityTrait,
        nest_hvac_pb2.RemoteComfortSensingSettingsTrait,
    )
}

# A thermostat publishes TemperatureTrait and HumidityTrait twice, and only the
# trait label tells them apart: `backplate_*` is its own sensor, `current_*` the
# reading it controls on. These are cached by label instead of by trait name,
# so one can't overwrite the other.
_LABEL_SPECIFIC_TRAITS: frozenset[str] = frozenset(
    {
        "backplate_temperature",
        "current_temperature",
        "backplate_humidity",
        "current_humidity",
    }
)

# Thermostat resource types and their models, from tronikos/nest_legacy.
_THERMOSTAT_MODELS: dict[str, str] = {
    "nest.resource.NestLearningThermostat1Resource": "Learning Thermostat (1st gen)",
    "nest.resource.NestLearningThermostat2Resource": "Learning Thermostat (2nd gen)",
    "nest.resource.NestAmber1DisplayResource": "Learning Thermostat (2nd gen)",
    "nest.resource.NestLearningThermostat3Resource": "Learning Thermostat (3rd gen)",
    "nest.resource.NestLearningThermostat3v2Resource": "Learning Thermostat (3rd gen)",
    "nest.resource.NestAmber2DisplayResource": "Learning Thermostat (3rd gen)",
    "nest.resource.NestOnyxResource": "Thermostat E",
    "nest.resource.NestAgateDisplayResource": "Thermostat E",
    "google.resource.GoogleZirconium1Resource": "Thermostat (2020)",
    "google.resource.GoogleBismuth1Resource": "Learning Thermostat (4th gen)",
    # homebridge-nest mounts this type as a thermostat; which model it is isn't
    # known.
    "nest.resource.NestThermostat3Resource": "Thermostat",
}

_RCS_SOURCE_TYPES: dict[int, TemperatureSource] = {
    nest_hvac_pb2.RemoteComfortSensingSettingsTrait.RcsSourceType.RCS_SOURCE_TYPE_BACKPLATE: TemperatureSource.BACKPLATE,
    nest_hvac_pb2.RemoteComfortSensingSettingsTrait.RcsSourceType.RCS_SOURCE_TYPE_SINGLE_SENSOR: TemperatureSource.SINGLE_SENSOR,
    nest_hvac_pb2.RemoteComfortSensingSettingsTrait.RcsSourceType.RCS_SOURCE_TYPE_MULTI_SENSOR: TemperatureSource.MULTI_SENSOR,
}

# Structure-level annotation traits — when they change we rebuild the
# wheres_map and re-emit every known lock so the location is picked up.
_STRUCTURE_ANNOTATION_TRAIT_NAMES: frozenset[str] = frozenset(
    {
        nest_located_pb2.LocatedAnnotationsTrait.DESCRIPTOR.full_name,
        nest_located_pb2.CustomLocatedAnnotationsTrait.DESCRIPTOR.full_name,
    }
)
_BOLT_LOCK_TRAIT_NAME = weave_security_pb2.BoltLockTrait.DESCRIPTOR.full_name
_STRUCTURE_INFO_TRAIT_NAME = nest_structure_pb2.StructureInfoTrait.DESCRIPTOR.full_name


def _decode_varint(buffer: bytes | bytearray) -> tuple[int | None, int]:
    """Decode a varint at the head of `buffer`. Returns (value, bytes_read)."""
    shift = 0
    result = 0
    bytes_read = 0
    while bytes_read < len(buffer):
        i = buffer[bytes_read]
        bytes_read += 1
        result |= (i & 0x7F) << shift
        shift += 7
        if not (i & 0x80):
            return result, bytes_read
    return None, 0


def _is_non_live_trait_label(trait_label: str) -> bool:
    """Return True for labels that never hold the device's current state.

    "_bucketized" copies are history, and "unpaired_*" copies are the settings
    a thermostat keeps for unpaired mode.
    """
    return trait_label.endswith("_bucketized") or trait_label.startswith("unpaired_")


def _is_auth_status(code: int, message: str) -> bool:
    """Return True if a gRPC status means the session credentials were rejected."""
    if code == _GRPC_UNAUTHENTICATED:
        return True
    return (
        code == _GRPC_PERMISSION_DENIED and "authentication failed" in message.lower()
    )


_ACTUATOR_STATE_MAP: dict[int, LockBoltState] = {
    weave_security_pb2.BoltLockTrait.BoltActuatorState.BOLT_ACTUATOR_STATE_LOCKING: LockBoltState.LOCKING,
    weave_security_pb2.BoltLockTrait.BoltActuatorState.BOLT_ACTUATOR_STATE_UNLOCKING: LockBoltState.UNLOCKING,
    weave_security_pb2.BoltLockTrait.BoltActuatorState.BOLT_ACTUATOR_STATE_JAMMED_UNLOCKING: LockBoltState.JAMMED,
    weave_security_pb2.BoltLockTrait.BoltActuatorState.BOLT_ACTUATOR_STATE_JAMMED_LOCKING: LockBoltState.JAMMED,
    weave_security_pb2.BoltLockTrait.BoltActuatorState.BOLT_ACTUATOR_STATE_JAMMED_OTHER: LockBoltState.JAMMED,
}
_LOCKED_STATE_MAP: dict[int, LockBoltState] = {
    weave_security_pb2.BoltLockTrait.BoltLockedState.BOLT_LOCKED_STATE_LOCKED: LockBoltState.LOCKED,
    weave_security_pb2.BoltLockTrait.BoltLockedState.BOLT_LOCKED_STATE_UNLOCKED: LockBoltState.UNLOCKED,
}


def _resolve_location(traits: dict[str, Any], wheres_map: dict[str, str]) -> str | None:
    """Resolve the room label for a device.

    Checks `DeviceLocatedSettingsTrait` in this order, mirroring nest_legacy:
    1. The denormalized `whereLabel.literal` / `fixtureNameLabel.literal`
       (set on most accounts but stripped in some delta updates).
    2. `whereAnnotationRid` / `fixtureAnnotationRid` looked up in `wheres_map`
       (which is populated from structure-level annotation traits).

    Returns None if no resolution succeeds.
    """
    loc_trait = traits.get(
        nest_located_pb2.DeviceLocatedSettingsTrait.DESCRIPTOR.full_name
    )
    if loc_trait is None:
        return None

    if loc_trait.HasField("whereLabel") and loc_trait.whereLabel.literal:
        return loc_trait.whereLabel.literal
    if loc_trait.HasField("fixtureNameLabel") and loc_trait.fixtureNameLabel.literal:
        return loc_trait.fixtureNameLabel.literal

    if loc_trait.HasField("whereAnnotationRid"):
        where_id = loc_trait.whereAnnotationRid.resourceId
        if where_id in wheres_map:
            return wheres_map[where_id]

    if loc_trait.HasField("fixtureAnnotationRid"):
        fixture_id = loc_trait.fixtureAnnotationRid.resourceId
        if fixture_id in wheres_map:
            return wheres_map[fixture_id]

    return None


def _extract_lock_state(
    resource_id: str,
    traits: dict[str, Any],
    wheres_map: dict[str, str] | None = None,
) -> LockState | None:
    """Build a LockState from a per-resource trait dict, or None if not a lock."""
    bolt_trait: weave_security_pb2.BoltLockTrait | None = traits.get(
        weave_security_pb2.BoltLockTrait.DESCRIPTOR.full_name
    )
    if not bolt_trait:
        return None

    bolt_state = _ACTUATOR_STATE_MAP.get(
        bolt_trait.actuatorState,
        _LOCKED_STATE_MAP.get(bolt_trait.lockedState, LockBoltState.UNKNOWN),
    )

    identity = traits.get(
        weave_description_pb2.DeviceIdentityTrait.DESCRIPTOR.full_name
    )
    # proto3 scalars have no presence, so a DeviceIdentityTrait that omits these
    # yields "" rather than None. An empty serial would collapse every lock into
    # one device registry entry, and an empty sw_version would be rendered.
    serial = (
        identity.serialNumber if identity and identity.serialNumber else resource_id
    )
    software_version = (
        identity.softwareVersion if identity and identity.softwareVersion else None
    )

    label = traits.get(weave_description_pb2.LabelSettingsTrait.DESCRIPTOR.full_name)
    name = label.label if label and label.label else "Lock"

    battery_level: float | None = None
    battery_trait = traits.get(
        weave_power_pb2.BatteryPowerSourceTrait.DESCRIPTOR.full_name
    )
    if battery_trait and battery_trait.HasField("remaining"):
        remaining = battery_trait.remaining
        if remaining.HasField("remainingPercent"):
            battery_level = 100.0 * remaining.remainingPercent.value

    location = _resolve_location(traits, wheres_map or {})

    return LockState(
        resource_id=resource_id,
        name=name,
        serial_number=serial,
        bolt_state=bolt_state,
        software_version=software_version,
        battery_level=battery_level,
        location=location,
    )


def _sensor_value(trait: Any, sample_field: str, value_field: str) -> float | None:
    """Read the sample of a TemperatureTrait or HumidityTrait, if it has one."""
    if trait is None or not trait.HasField(sample_field):
        return None
    sample = getattr(trait, sample_field)
    if not sample.HasField(value_field):
        return None
    return getattr(sample, value_field).value


def _current_reading(traits: dict[str, Any], label: str, trait_class: type) -> Any:
    """Return the trait a thermostat controls on, by label if it has one.

    Falls back to an instance under another label, like nest_legacy does, so a
    thermostat that labels it differently still reports a reading.
    """
    trait = traits.get(label)
    return trait if trait is not None else traits.get(trait_class.DESCRIPTOR.full_name)


def _extract_thermostat_state(
    resource_id: str,
    resource_type: str | None,
    traits: dict[str, Any],
    wheres_map: dict[str, str],
) -> ThermostatState | None:
    """Build a ThermostatState, or None if not a thermostat or not identified yet."""
    model = _THERMOSTAT_MODELS.get(resource_type or "")
    if model is None:
        return None

    identity = traits.get(
        weave_description_pb2.DeviceIdentityTrait.DESCRIPTOR.full_name
    )
    # DeviceInfo is only read when an entity is added, so registering before the
    # serial number arrives would leave the device without its real identifier.
    if not identity or not identity.serialNumber:
        return None

    label = traits.get(weave_description_pb2.LabelSettingsTrait.DESCRIPTOR.full_name)

    source: TemperatureSource | None = None
    active_sensors: list[str] = []
    rcs = traits.get(
        nest_hvac_pb2.RemoteComfortSensingSettingsTrait.DESCRIPTOR.full_name
    )
    if rcs is not None:
        selection = rcs.activeRcsSelection
        source = _RCS_SOURCE_TYPES.get(selection.rcsSourceType)
        if source is TemperatureSource.MULTI_SENSOR:
            active_sensors = [
                sensor.resourceId
                for sensor in rcs.multiSensorSettings.multiSensorGroup
                if sensor.resourceId
            ]
        elif (
            source is TemperatureSource.SINGLE_SENSOR
            and selection.activeRcsSensor.resourceId
        ):
            active_sensors = [selection.activeRcsSensor.resourceId]

    return ThermostatState(
        resource_id=resource_id,
        serial_number=identity.serialNumber,
        model=model,
        name=label.label if label and label.label else None,
        software_version=identity.softwareVersion or None,
        location=_resolve_location(traits, wheres_map),
        backplate_temperature=_sensor_value(
            traits.get("backplate_temperature"), "temperatureValue", "temperature"
        ),
        current_temperature=_sensor_value(
            _current_reading(
                traits, "current_temperature", nest_sensor_pb2.TemperatureTrait
            ),
            "temperatureValue",
            "temperature",
        ),
        current_humidity=_sensor_value(
            _current_reading(traits, "current_humidity", nest_sensor_pb2.HumidityTrait),
            "humidityValue",
            "humidity",
        ),
        temperature_source=source,
        active_sensors=active_sensors,
    )


@dataclass
class ObserveBatch:
    """Devices whose state changed in one observe update."""

    locks: dict[str, LockState] = field(default_factory=dict)
    thermostats: dict[str, ThermostatState] = field(default_factory=dict)


class GrpcClient:
    """gRPC-web client for the gateway's observe stream and commands."""

    def __init__(
        self, nest_client: NestClient, grpc_host: str = "grpc-web.production.nest.com"
    ) -> None:
        """Initialize.

        `nest_client` is ha-nest-protect's existing NestClient — we read
        `nest_client.nest_session.access_token` on every call to pick up
        token refreshes done by NestSessionManager.
        """
        self._nest_client = nest_client
        self._grpc_host = grpc_host
        # Per-resource trait cache: resource_id -> {trait_full_name: trait_proto}
        self._trait_cache: dict[str, dict[str, Any]] = {}
        # resource_id -> resource type, from the gateway's resource metas.
        self._resource_types: dict[str, str] = {}
        # Global where_id -> room label, rebuilt whenever the structure-level
        # annotation traits change. Devices use this to resolve their location.
        self._wheres_map: dict[str, str] = {}
        # Tri-state: None until the gateway has told us enough to decide, then
        # True once any lock or thermostat is seen, or False once the initial
        # enumeration has settled without one. See `_evaluate_device_presence`.
        self._devices_present: bool | None = None
        # Whether the gateway has sent any resourceMetas yet, and whether it
        # says more of the initial batch is still coming.
        self._seen_resource_metas = False
        self._initial_metas_continue = False
        # Monotonic timestamp of the first observe attempt, used for the settle
        # window. Set once and kept across reconnects.
        self._first_observe_at: float | None = None

    def structure_resource_id(self, structure_key: str) -> str | None:
        """Return the protobuf resource id of the REST bucket `structure.<id>`.

        Known once the observe stream has sent the structure's
        StructureInfoTrait, which is part of every stream's initial state.
        """
        for resource_id, traits in self._trait_cache.items():
            info = traits.get(_STRUCTURE_INFO_TRAIT_NAME)
            if info is not None and info.rtsStructureId == structure_key:
                return resource_id
        return None

    def _headers(self) -> dict[str, str]:
        """Build the protobuf headers using the current session token."""
        session = self._nest_client.nest_session
        if session is None or not session.access_token:
            raise NestLockAuthException("No active Nest session — cannot call gRPC-web")
        # NestEnvironment.host already carries the scheme ("https://home.nest.com"),
        # which is why client.py uses it bare.
        host = self._nest_client.environment.host
        return {
            "Authorization": f"Basic {session.access_token}",
            "User-Agent": PROTOBUF_USER_AGENT,
            "Content-Type": "application/x-protobuf",
            "X-Accept-Response-Streaming": "true",
            "X-Accept-Content-Transfer-Encoding": "binary",
            "Referer": f"{host}/",
            "Origin": host,
        }

    def _build_observe_request(self) -> bytes:
        """Serialize an ObserveRequest filtered to lock-relevant traits."""
        req = v2_pb2.ObserveRequest(
            stateTypes=[v2_pb2.ACCEPTED, v2_pb2.CONFIRMED],
            traitTypeParams=[
                v2_pb2.TraitTypeObserveParams(traitType=name)
                for name in _TRAIT_NAME_TO_CLASS
            ],
        )
        return req.SerializeToString()

    def _ingest_observe_response(
        self, inner: v2_pb2.ObserveResponse.ObserveResponse
    ) -> set[str]:
        """Apply trait updates from one inner ObserveResponse to the cache.

        Returns the set of resource_ids that need re-emission. That includes
        directly-touched resources, thermostats whose type just became known
        (their traits may have arrived first), plus, if a structure-level
        annotation trait changed, every cached lock and thermostat (so the new
        location propagates).
        """
        touched = self._ingest_resource_metas(inner)
        annotations_changed = False
        for state in inner.traitStates:
            type_url = state.patch.values.type_url
            full_name = type_url.removeprefix(_NESTLABS_TYPE_URL_PREFIX)
            target_class = _TRAIT_NAME_TO_CLASS.get(full_name)
            if target_class is None:
                continue

            label = state.traitId.traitLabel
            if _is_non_live_trait_label(label):
                continue

            unpacked = target_class()
            state.patch.values.Unpack(unpacked)

            resource_id = state.traitId.resourceId
            cache_entry = self._trait_cache.setdefault(resource_id, {})
            cache_entry[label if label in _LABEL_SPECIFIC_TRAITS else full_name] = (
                unpacked
            )

            if full_name == _BOLT_LOCK_TRAIT_NAME:
                self._devices_present = True

            if full_name in _STRUCTURE_ANNOTATION_TRAIT_NAMES:
                annotations_changed = True
            else:
                touched.add(resource_id)

        if annotations_changed:
            self._rebuild_wheres_map()
            for rid, cached_traits in self._trait_cache.items():
                if (
                    _BOLT_LOCK_TRAIT_NAME in cached_traits
                    or self._resource_types.get(rid) in _THERMOSTAT_MODELS
                ):
                    touched.add(rid)

        return touched

    def _ingest_resource_metas(
        self, inner: v2_pb2.ObserveResponse.ObserveResponse
    ) -> set[str]:
        """Record resource types; return thermostats whose type just became known."""
        touched: set[str] = set()
        if not inner.resourceMetas:
            return touched

        self._seen_resource_metas = True
        self._initial_metas_continue = inner.initialResourceMetasContinue
        for meta in inner.resourceMetas:
            if meta.type in _THERMOSTAT_MODELS:
                self._devices_present = True
                if self._resource_types.get(meta.resourceId) != meta.type:
                    touched.add(meta.resourceId)
            if meta.type and meta.resourceId not in self._resource_types:
                # Thermostat discovery depends on this type, so it is worth
                # seeing in debug logs when a device doesn't show up.
                _LOGGER.debug("Observe: %s is a %s", meta.resourceId, meta.type)
            if meta.type:
                self._resource_types[meta.resourceId] = meta.type
            for trait_meta in meta.traitMetas:
                name = trait_meta.type.removeprefix(_NESTLABS_TYPE_URL_PREFIX)
                if name == _BOLT_LOCK_TRAIT_NAME:
                    self._devices_present = True
        return touched

    def _rebuild_wheres_map(self) -> None:
        """Rebuild `_wheres_map` from any cached annotation traits."""
        wheres: dict[str, str] = {}
        ann_name = nest_located_pb2.LocatedAnnotationsTrait.DESCRIPTOR.full_name
        custom_name = (
            nest_located_pb2.CustomLocatedAnnotationsTrait.DESCRIPTOR.full_name
        )

        for traits in self._trait_cache.values():
            ann_trait = traits.get(ann_name)
            if ann_trait is not None:
                for item in ann_trait.predefinedWheres.values():
                    if item.HasField("whereId") and item.HasField("label"):
                        wheres[item.whereId.resourceId] = item.label.literal
                for item in ann_trait.customWheres.values():
                    if item.HasField("whereId") and item.HasField("label"):
                        wheres[item.whereId.resourceId] = item.label.literal

            custom_trait = traits.get(custom_name)
            if custom_trait is not None:
                for w_item in custom_trait.wheresList.values():
                    if w_item.HasField("whereId") and w_item.HasField("label"):
                        wheres[w_item.whereId.resourceId] = w_item.label.literal
                for f_item in custom_trait.fixturesList.values():
                    if f_item.HasField("fixtureId") and f_item.HasField("label"):
                        wheres[f_item.fixtureId.resourceId] = f_item.label.literal

        self._wheres_map = wheres

    def _parse_observe_buffer(self, buffer: bytearray) -> list[set[str]]:
        """Drain complete `google.rpc.StreamBody` frames from `buffer`.

        The gateway streams its response as a sequence of length-delimited
        StreamBody fields. A `message` entry (field 1) holds one serialized
        `ObserveResponse.ObserveResponse`; a `status` entry (field 2) carries a
        gateway-side error for the stream as a whole, and raises
        `NestLockAuthException` when it rejects the session.

        Returns one set of touched resource_ids per entry that changed
        something, so the caller can snapshot just those resources.
        """
        results: list[set[str]] = []
        while buffer:
            tag, tag_size = _decode_varint(buffer)
            if tag is None:
                break

            wire_type = tag & 0x07
            if wire_type != _WIRE_TYPE_LENGTH_DELIMITED:
                _LOGGER.debug(
                    "Unexpected wire type %s in observe stream; resetting buffer",
                    wire_type,
                )
                buffer.clear()
                break

            length, length_size = _decode_varint(buffer[tag_size:])
            if length is None:
                break

            frame_size = tag_size + length_size + length
            if len(buffer) < frame_size:
                break

            frame_data = bytes(buffer[:frame_size])
            del buffer[:frame_size]

            body = streambody_pb2.StreamBody()
            try:
                body.ParseFromString(frame_data)
            except Exception:
                _LOGGER.exception("Failed to parse observe StreamBody frame")
                continue

            if body.HasField("status") and _is_auth_status(
                body.status.code, body.status.message
            ):
                raise NestLockAuthException(
                    f"Observe stream rejected: code={body.status.code} "
                    f"message={body.status.message!r}"
                )
            if body.HasField("status") and body.status.code != 0:
                # The gateway ends every observe stream after about an hour with
                # DEADLINE_EXCEEDED. That's the normal end of a long-lived stream,
                # and the caller reconnects straight away.
                log = (
                    _LOGGER.debug
                    if body.status.code == _GRPC_DEADLINE_EXCEEDED
                    else _LOGGER.warning
                )
                log(
                    "Observe stream reported status code=%s message=%r",
                    body.status.code,
                    body.status.message,
                )

            for raw_response in body.message:
                inner = v2_pb2.ObserveResponse.ObserveResponse()
                try:
                    inner.ParseFromString(raw_response)
                except Exception:
                    _LOGGER.exception("Failed to parse ObserveResponse")
                    continue

                touched = self._ingest_observe_response(inner)
                if touched:
                    results.append(touched)
        return results

    def _snapshot(self, touched: set[str]) -> ObserveBatch:
        """Extract the device states for each touched resource from the cache."""
        batch = ObserveBatch()
        for rid in touched:
            traits = self._trait_cache.get(rid)
            if not traits:
                continue
            if lock := _extract_lock_state(rid, traits, self._wheres_map):
                batch.locks[rid] = lock
            elif thermostat := _extract_thermostat_state(
                rid, self._resource_types.get(rid), traits, self._wheres_map
            ):
                batch.thermostats[rid] = thermostat
        return batch

    def _evaluate_device_presence(self, now: float) -> None:
        """Decide whether this account has no locks or thermostats at all.

        Called after each chunk. Flips `_devices_present` from undetermined to
        False once the gateway has finished its initial resource enumeration
        without ever mentioning a `BoltLockTrait` or a thermostat, and the settle
        window has passed. Never overrides a True — one device is enough.
        """
        if self._devices_present is not None:
            return
        if not self._seen_resource_metas or self._initial_metas_continue:
            return
        if self._first_observe_at is None:
            return
        if now - self._first_observe_at < _NO_DEVICE_SETTLE_SECONDS:
            return
        self._devices_present = False

    async def observe(self) -> AsyncIterator[ObserveBatch]:
        """Long-lived observer. Yields an `ObserveBatch` per update.

        Reconnects on transient errors with exponential backoff. Ends the
        iteration — rather than reconnecting forever — once the account is
        known to have no lock or thermostat, so those accounts stop talking to
        the gateway after the first stream. Auth failures propagate to the caller
        instead of being retried, since retrying a rejected token never
        recovers. Caller is responsible for cancelling the consuming task on
        shutdown.
        """
        delay = _RECONNECT_INITIAL_DELAY
        while True:
            started = asyncio.get_running_loop().time()
            try:
                async for batch in self._observe_once():
                    if (
                        asyncio.get_running_loop().time() - started
                        >= _STREAM_HEALTHY_SECONDS
                    ):
                        delay = _RECONNECT_INITIAL_DELAY
                    yield batch
                # Re-check on the way out too: a stream that ends right after
                # the enumeration would otherwise need another connection
                # before the verdict could be reached.
                self._evaluate_device_presence(asyncio.get_running_loop().time())
                if self._devices_present is False:
                    return
                # A clean end isn't an error, but still back off. An account
                # with no device resources can get the stream closed immediately,
                # which would otherwise reconnect once a second forever.
                _LOGGER.debug("Observe stream ended; reconnecting in %.1fs", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX_DELAY)
            except asyncio.CancelledError:
                raise
            except NestLockAuthException:
                raise
            except SocketTimeoutError:
                # The connection was up but stayed silent for _SOCK_READ_TIMEOUT:
                # the devices were simply quiet. Re-open without growing the backoff;
                # the timeout itself already spaces these out.
                _LOGGER.debug(
                    "Observe stream idle for %ss; reconnecting", _SOCK_READ_TIMEOUT
                )
                delay = _RECONNECT_INITIAL_DELAY
                await asyncio.sleep(delay)
            except Exception as err:  # noqa: BLE001
                # Until a device is known to exist, stream trouble isn't
                # actionable for the user — most accounts have none, and
                # their first stream idles out before the presence check can
                # settle. Only warn once we know there is something to watch.
                log = _LOGGER.warning if self._devices_present else _LOGGER.debug
                log(
                    "Observe stream error: %r. Reconnecting in %.1fs",
                    err,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX_DELAY)

    async def _observe_once(self) -> AsyncIterator[ObserveBatch]:
        """Single observe-stream session. Yields batches until the stream ends."""
        url = f"https://{self._grpc_host}{OBSERVE_ENDPOINT}"
        body = self._build_observe_request()
        loop = asyncio.get_running_loop()
        if self._first_observe_at is None:
            self._first_observe_at = loop.time()
        # The gateway re-enumerates resources on every new stream.
        self._seen_resource_metas = False
        self._initial_metas_continue = False
        async with self._nest_client.session.post(
            url,
            data=body,
            headers=self._headers(),
            timeout=ClientTimeout(
                total=None, connect=_CONNECT_TIMEOUT, sock_read=_SOCK_READ_TIMEOUT
            ),
        ) as response:
            if response.status in _AUTH_ERROR_STATUSES:
                raise NestLockAuthException(
                    f"Observe rejected with HTTP {response.status}"
                )
            response.raise_for_status()
            buffer = bytearray()
            async for chunk in response.content.iter_chunked(4096):
                if not chunk:
                    break
                buffer.extend(chunk)
                for touched in self._parse_observe_buffer(buffer):
                    batch = self._snapshot(touched)
                    if batch.locks or batch.thermostats:
                        yield batch
                self._evaluate_device_presence(loop.time())
                if self._devices_present is False:
                    _LOGGER.debug(
                        "Observe enumeration completed with no lock or "
                        "thermostat on this account. Reload the integration "
                        "if you add one later"
                    )
                    return

    async def send_lock_command(self, resource_id: str, lock: bool) -> None:
        """Send a lock or unlock command. Raises on failure."""
        state_value = (
            weave_security_pb2.BoltLockTrait.BoltState.BOLT_STATE_EXTENDED
            if lock
            else weave_security_pb2.BoltLockTrait.BoltState.BOLT_STATE_RETRACTED
        )
        change_req = weave_security_pb2.BoltLockTrait.BoltLockChangeRequest(
            state=state_value,
            boltLockActor=weave_security_pb2.BoltLockTrait.BoltLockActorStruct(
                method=weave_security_pb2.BoltLockTrait.BoltLockActorMethod.BOLT_LOCK_ACTOR_METHOD_REMOTE_USER_EXPLICIT
            ),
        )
        command = v1_pb2.ResourceCommand(traitLabel="bolt_lock")
        command.command.Pack(change_req, type_url_prefix=_NESTLABS_TYPE_URL_PREFIX)
        await self._send_command(resource_id, command, "Lock command")

    async def send_structure_mode_command(
        self, structure_resource_id: str, away: bool
    ) -> None:
        """Set a structure to Home or Away. Raises on failure."""
        mode_trait = nest_occupancy_pb2.StructureModeTrait
        change_req = mode_trait.StructureModeChangeRequest(
            structureMode=(
                mode_trait.StructureMode.STRUCTURE_MODE_AWAY
                if away
                else mode_trait.StructureMode.STRUCTURE_MODE_HOME
            ),
            reason=mode_trait.StructureModeReason.STRUCTURE_MODE_REASON_EXPLICIT_INTENT,
        )
        command = v1_pb2.ResourceCommand(traitLabel="structure_mode")
        command.command.Pack(change_req, type_url_prefix=_NESTLABS_TYPE_URL_PREFIX)
        await self._send_command(
            structure_resource_id, command, "Structure mode command"
        )

    async def _send_command(
        self, resource_id: str, command: v1_pb2.ResourceCommand, what: str
    ) -> None:
        """Send one resource command. Raises on failure."""
        send_req = v1_pb2.SendCommandRequest(
            resourceRequest=v1_pb2.ResourceRequest(
                resourceId=resource_id, requestId=str(uuid.uuid4())
            ),
            resourceCommands=[command],
        )
        body = send_req.SerializeToString()
        url = f"https://{self._grpc_host}{SEND_COMMAND_ENDPOINT}"

        async with self._nest_client.session.post(
            url,
            data=body,
            headers=self._headers(),
            timeout=ClientTimeout(total=_SEND_COMMAND_TIMEOUT),
        ) as response:
            if response.status in _AUTH_ERROR_STATUSES:
                raise NestLockAuthException(
                    f"SendCommand rejected with HTTP {response.status}"
                )
            if not response.ok:
                text = await response.text()
                raise NestLockCommandException(
                    f"SendCommand HTTP {response.status}: {text[:200]}"
                )
            raw = await response.read()

        resp = v1_pb2.SendCommandResponse()
        resp.ParseFromString(raw)
        _LOGGER.debug(
            "SendCommand response for %s: code=%s message=%r",
            resource_id,
            resp.status.code,
            resp.status.message,
        )
        if _is_auth_status(resp.status.code, resp.status.message):
            raise NestLockAuthException(
                f"{what} rejected: code={resp.status.code} message={resp.status.message!r}"
            )
        if resp.status.code != 0:
            raise NestLockCommandException(
                f"{what} rejected: code={resp.status.code} message={resp.status.message!r}"
            )
