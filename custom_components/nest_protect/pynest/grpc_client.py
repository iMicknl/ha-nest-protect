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

from .const import PROTOBUF_USER_AGENT
from .exceptions import NestLockAuthException, NestLockCommandException
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

_SOCK_READ_TIMEOUT = 300  # seconds without data before treating the stream as dead
_CONNECT_TIMEOUT = 60
_SEND_COMMAND_TIMEOUT = 30
_RECONNECT_INITIAL_DELAY = 1.0
_RECONNECT_MAX_DELAY = 60.0

# How long the observe stream must have been running, cumulatively across
# reconnects, before an account is declared lock-less. The gateway sends its
# resource metas and the initial trait states in the first moments of a stream,
# but the ordering between the two isn't guaranteed, so a settle window keeps a
# slow initial dump from being mistaken for "no locks here".
_NO_LOCK_SETTLE_SECONDS = 30.0

# HTTP statuses that mean the session token was rejected rather than that the
# gateway had a transient problem. Retrying these forever never recovers.
_AUTH_ERROR_STATUSES = frozenset({401, 403})

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
        # Tri-state: None until the gateway has told us enough to decide, then
        # True once any BoltLockTrait is seen, or False once the initial
        # enumeration has settled without one. See `_evaluate_lock_presence`.
        self._locks_present: bool | None = None
        # Whether the gateway has sent any resourceMetas yet, and whether it
        # says more of the initial batch is still coming.
        self._seen_resource_metas = False
        self._initial_metas_continue = False
        # Monotonic timestamp of the first observe attempt, used for the settle
        # window. Set once and kept across reconnects.
        self._first_observe_at: float | None = None

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
        directly-touched lock resources plus, if a structure-level annotation
        trait changed, every cached lock (so the new location propagates).
        """
        if inner.resourceMetas:
            self._seen_resource_metas = True
            self._initial_metas_continue = inner.initialResourceMetasContinue
            for meta in inner.resourceMetas:
                for trait_meta in meta.traitMetas:
                    name = trait_meta.type.removeprefix(_NESTLABS_TYPE_URL_PREFIX)
                    if name == _BOLT_LOCK_TRAIT_NAME:
                        self._locks_present = True

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

            if full_name == _BOLT_LOCK_TRAIT_NAME:
                self._locks_present = True

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
        """Drain complete frames from `buffer`, returning lists of touched resource sets.

        The response body is a stream of `google.rpc.StreamBody` frames, each of
        which is a length-delimited `repeated bytes message = 1` entry.

        Note that the frame is handed to `ObserveResponse.ParseFromString()`
        *including* its own tag and length prefix, rather than the payload
        alone. That is deliberate and not a slicing bug: `StreamBody.message`
        and `ObserveResponse.observeResponse` are both field number 1 with
        wire type 2, so parsing the wrapper as an `ObserveResponse` makes
        protobuf reinterpret the StreamBody envelope as the repeated
        `observeResponse` field. This saves vendoring `StreamBody` itself,
        at the cost of depending on those two field numbers staying aligned.
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

    def _evaluate_lock_presence(self, now: float) -> None:
        """Decide whether this account has no locks at all.

        Called after each chunk. Flips `_locks_present` from undetermined to
        False once the gateway has finished its initial resource enumeration
        without ever mentioning a `BoltLockTrait`, and the settle window has
        passed. Never overrides a True — one BoltLockTrait anywhere is enough.
        """
        if self._locks_present is not None:
            return
        if not self._seen_resource_metas or self._initial_metas_continue:
            return
        if self._first_observe_at is None:
            return
        if now - self._first_observe_at < _NO_LOCK_SETTLE_SECONDS:
            return
        self._locks_present = False

    async def observe_locks(self) -> AsyncIterator[dict[str, LockState]]:
        """Long-lived observer. Yields `{resource_id: LockState}` per update batch.

        Reconnects on transient errors with exponential backoff. Ends the
        iteration — rather than reconnecting forever — once the account is
        known to have no locks, so accounts without a Nest x Yale lock stop
        talking to the gateway entirely. Auth failures propagate to the caller
        instead of being retried, since retrying a rejected token never
        recovers. Caller is responsible for cancelling the consuming task on
        shutdown.
        """
        delay = _RECONNECT_INITIAL_DELAY
        while True:
            try:
                async for batch in self._observe_once():
                    delay = _RECONNECT_INITIAL_DELAY
                    yield batch
                # Re-check on the way out too: a stream that ends right after
                # the enumeration would otherwise need another connection
                # before the verdict could be reached.
                self._evaluate_lock_presence(asyncio.get_running_loop().time())
                if self._locks_present is False:
                    return
                # A clean end isn't an error, but still back off. An account
                # with no lock resources can get the stream closed immediately,
                # which would otherwise reconnect once a second forever.
                _LOGGER.debug("Observe stream ended; reconnecting in %.1fs", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX_DELAY)
            except asyncio.CancelledError:
                raise
            except NestLockAuthException:
                raise
            except Exception as err:  # noqa: BLE001
                # Until a lock is known to exist, stream trouble isn't
                # actionable for the user — most accounts have no lock, and
                # their first stream idles out before the presence check can
                # settle. Only warn once we know there is something to watch.
                log = _LOGGER.warning if self._locks_present else _LOGGER.debug
                log(
                    "Lock observe stream error: %r. Reconnecting in %.1fs",
                    err,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX_DELAY)

    async def _observe_once(self) -> AsyncIterator[dict[str, LockState]]:
        """Single observe-stream session. Yields LockState batches until stream ends."""
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
                    locks = self._snapshot_locks(touched)
                    if locks:
                        yield locks
                self._evaluate_lock_presence(loop.time())
                if self._locks_present is False:
                    _LOGGER.debug(
                        "Observe enumeration completed with no BoltLockTrait; "
                        "no Nest x Yale lock on this account. Reload the "
                        "integration if you add one later"
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
        if resp.status.code != 0:
            raise NestLockCommandException(
                f"Lock command rejected: code={resp.status.code} message={resp.status.message!r}"
            )
