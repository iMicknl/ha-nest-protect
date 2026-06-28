"""gRPC-web client for Nest x Yale lock state and commands.

Talks to nestlabs.gateway.v2.GatewayService/Observe (streaming state) and
nestlabs.gateway.v1.ResourceApi/SendCommand (lock/unlock), both at
grpc-web.production.nest.com. Reuses the ha-nest-protect session's
Basic access_token verbatim — no separate JWT issuance is required.

This is a focused port of the lock-relevant slice of tronikos/nest_legacy.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from aiohttp import ClientTimeout

from .lock_models import LockBoltState, LockState
from .protobuf_gen.nest.trait import located_pb2 as nest_located_pb2
from .protobuf_gen.nestlabs.gateway import v1_pb2, v2_pb2
from .protobuf_gen.weave.trait import description_pb2 as weave_description_pb2
from .protobuf_gen.weave.trait import power_pb2 as weave_power_pb2
from .protobuf_gen.weave.trait import security_pb2 as weave_security_pb2

if TYPE_CHECKING:
    from .client import NestClient

_LOGGER = logging.getLogger(__name__)

OBSERVE_ENDPOINT = "/nestlabs.gateway.v2.GatewayService/Observe"
SEND_COMMAND_ENDPOINT = "/nestlabs.gateway.v1.ResourceApi/SendCommand"

_NESTLABS_TYPE_URL_PREFIX = "type.nestlabs.com/"
_USER_AGENT = "Nest/5.82.2 (iOScom.nestlabs.jasper.release) os=18.5"

_OBSERVE_TIMEOUT = 600  # seconds — long-lived stream
_CONNECT_TIMEOUT = 60
_SEND_COMMAND_TIMEOUT = 30
_RECONNECT_INITIAL_DELAY = 1.0
_RECONNECT_MAX_DELAY = 60.0

# Protobuf wire-type for length-delimited fields. The Observe stream wraps
# each ObserveResponse in a length-delimited field (tag wire-type == 2);
# any other wire-type means the buffer is out of sync and must be reset.
_WIRE_TYPE_LENGTH_DELIMITED = 2

# The lock-relevant trait types we ask the server to stream. Limited set keeps
# bandwidth and parsing cost down vs subscribing to the entire trait surface.
# The three nest.trait.located traits are needed to resolve human-readable
# room labels: DeviceLocatedSettingsTrait lives on each lock, the two
# *Annotations traits live on the structure resource as a where_id → label
# catalogue.
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
    )
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


def _resolve_lock_location(
    traits: dict[str, Any], wheres_map: dict[str, str]
) -> str | None:
    """Resolve the room label for a lock.

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
    serial = identity.serialNumber if identity else resource_id
    software_version = identity.softwareVersion if identity else None

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

    location = _resolve_lock_location(traits, wheres_map or {})

    return LockState(
        resource_id=resource_id,
        name=name,
        serial_number=serial,
        bolt_state=bolt_state,
        software_version=software_version,
        battery_level=battery_level,
        location=location,
    )


class GrpcLockClient:
    """gRPC-web client scoped to Nest x Yale lock observe + command."""

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
        # Global where_id -> room label, rebuilt whenever the structure-level
        # annotation traits change. Locks use this to resolve their location.
        self._wheres_map: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        """Build the protobuf headers using the current session token."""
        session = self._nest_client.nest_session
        if session is None or not session.access_token:
            raise RuntimeError("No active Nest session — cannot call gRPC-web")
        return {
            "Authorization": f"Basic {session.access_token}",
            "User-Agent": _USER_AGENT,
            "Content-Type": "application/x-protobuf",
            "X-Accept-Response-Streaming": "true",
            "X-Accept-Content-Transfer-Encoding": "binary",
            "Referer": f"https://{self._nest_client.environment.host}/",
            "Origin": f"https://{self._nest_client.environment.host}",
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
        directly-touched lock resources plus, if a structure-level annotation
        trait changed, every cached lock (so the new location propagates).
        """
        touched: set[str] = set()
        annotations_changed = False
        for state in inner.traitStates:
            type_url = state.patch.values.type_url
            full_name = type_url.removeprefix(_NESTLABS_TYPE_URL_PREFIX)
            target_class = _TRAIT_NAME_TO_CLASS.get(full_name)
            if target_class is None:
                continue

            unpacked = target_class()
            state.patch.values.Unpack(unpacked)

            resource_id = state.traitId.resourceId
            cache_entry = self._trait_cache.setdefault(resource_id, {})
            cache_entry[full_name] = unpacked

            if full_name in _STRUCTURE_ANNOTATION_TRAIT_NAMES:
                annotations_changed = True
            else:
                touched.add(resource_id)

        if annotations_changed:
            self._rebuild_wheres_map()
            for rid, cached_traits in self._trait_cache.items():
                if _BOLT_LOCK_TRAIT_NAME in cached_traits:
                    touched.add(rid)

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
        """Drain complete frames from `buffer`, returning lists of touched resource sets."""
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

            if tag >> 3 != 1:
                _LOGGER.debug("Skipping unknown field tag %s", tag >> 3)
                continue

            outer = v2_pb2.ObserveResponse()
            try:
                outer.ParseFromString(frame_data)
            except Exception:
                _LOGGER.exception("Failed to parse outer ObserveResponse")
                continue

            for inner in outer.observeResponse:
                touched = self._ingest_observe_response(inner)
                if touched:
                    results.append(touched)
        return results

    def _snapshot_locks(self, touched: set[str]) -> dict[str, LockState]:
        """Extract LockState for each touched resource from the cache."""
        out: dict[str, LockState] = {}
        for rid in touched:
            traits = self._trait_cache.get(rid)
            if not traits:
                continue
            lock = _extract_lock_state(rid, traits, self._wheres_map)
            if lock is not None:
                out[rid] = lock
        return out

    async def observe_locks(self) -> AsyncIterator[dict[str, LockState]]:
        """Long-lived observer. Yields `{resource_id: LockState}` per update batch.

        Reconnects on transient errors with exponential backoff. Caller is
        responsible for cancelling the consuming task on shutdown.
        """
        delay = _RECONNECT_INITIAL_DELAY
        while True:
            try:
                async for batch in self._observe_once():
                    delay = _RECONNECT_INITIAL_DELAY
                    yield batch
                # Clean stream end — short pause then reconnect.
                _LOGGER.debug("Observe stream ended cleanly; reconnecting")
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Lock observe stream error: %s. Reconnecting in %.1fs",
                    err,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX_DELAY)

    async def _observe_once(self) -> AsyncIterator[dict[str, LockState]]:
        """Single observe-stream session. Yields LockState batches until stream ends."""
        url = f"https://{self._grpc_host}{OBSERVE_ENDPOINT}"
        body = self._build_observe_request()
        async with self._nest_client.session.post(
            url,
            data=body,
            headers=self._headers(),
            timeout=ClientTimeout(total=_OBSERVE_TIMEOUT, connect=_CONNECT_TIMEOUT),
        ) as response:
            response.raise_for_status()
            buffer = bytearray()
            async for chunk in response.content.iter_chunked(4096):
                if not chunk:
                    break
                buffer.extend(chunk)
                for touched in self._parse_observe_buffer(buffer):
                    locks = self._snapshot_locks(touched)
                    if locks:
                        yield locks

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
            if not response.ok:
                text = await response.text()
                raise RuntimeError(f"SendCommand HTTP {response.status}: {text[:200]}")
            raw = await response.read()

        resp = v1_pb2.SendCommandResponse()
        resp.ParseFromString(raw)
        _LOGGER.debug(
            "SendCommand response for %s: code=%s message=%r",
            resource_id,
            resp.status.code,
            resp.status.message,
        )
        if resp.status.code != 0:
            raise RuntimeError(
                f"Lock command rejected: code={resp.status.code} message={resp.status.message!r}"
            )
