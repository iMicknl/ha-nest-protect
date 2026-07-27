"""Tests for the Nest x Yale lock gRPC-web client."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import ClientError, ClientResponseError

from custom_components.nest_protect.lock import _compose_lock_device_name
from custom_components.nest_protect.pynest.const import PROTOBUF_USER_AGENT
from custom_components.nest_protect.pynest.exceptions import (
    NestLockAuthException,
    NestLockCommandException,
)
from custom_components.nest_protect.pynest.grpc_client import (
    GrpcLockClient,
    _decode_varint,
    _extract_lock_state,
    _resolve_lock_location,
)
from custom_components.nest_protect.pynest.lock_models import LockBoltState
from custom_components.nest_protect.pynest.protobuf_gen.nest.trait import (
    located_pb2 as nest_located_pb2,
)
from custom_components.nest_protect.pynest.protobuf_gen.nestlabs.gateway import (
    v1_pb2,
    v2_pb2,
)
from custom_components.nest_protect.pynest.protobuf_gen.weave.trait import (
    description_pb2 as weave_description_pb2,
)
from custom_components.nest_protect.pynest.protobuf_gen.weave.trait import (
    power_pb2 as weave_power_pb2,
)
from custom_components.nest_protect.pynest.protobuf_gen.weave.trait import (
    security_pb2 as weave_security_pb2,
)

# -- _decode_varint ---------------------------------------------------------


@pytest.mark.parametrize(
    ("buffer", "expected_value", "expected_bytes"),
    [
        (b"\x00", 0, 1),
        (b"\x01", 1, 1),
        (b"\x7f", 127, 1),
        (b"\x80\x01", 128, 2),
        (b"\xff\x01", 255, 2),
        (b"\x92\x06", 786, 2),  # observed in spike output: outer ObserveResponse length
        (b"\xff\xff\xff\x7f", (1 << 28) - 1, 4),
    ],
)
def test_decode_varint_valid(buffer, expected_value, expected_bytes):
    value, consumed = _decode_varint(buffer)
    assert value == expected_value
    assert consumed == expected_bytes


def test_decode_varint_empty_buffer():
    assert _decode_varint(b"") == (None, 0)


def test_decode_varint_incomplete():
    # Continuation bit set but no more bytes
    assert _decode_varint(b"\x80") == (None, 0)


# -- _extract_lock_state ----------------------------------------------------


def _make_bolt_trait(
    locked_state: int = weave_security_pb2.BoltLockTrait.BoltLockedState.BOLT_LOCKED_STATE_LOCKED,
    actuator_state: int = weave_security_pb2.BoltLockTrait.BoltActuatorState.BOLT_ACTUATOR_STATE_OK,
) -> weave_security_pb2.BoltLockTrait:
    trait = weave_security_pb2.BoltLockTrait()
    trait.lockedState = locked_state
    trait.actuatorState = actuator_state
    return trait


def test_extract_lock_state_locked():
    traits = {
        weave_security_pb2.BoltLockTrait.DESCRIPTOR.full_name: _make_bolt_trait(),
    }
    lock = _extract_lock_state("DEVICE_X", traits)
    assert lock is not None
    assert lock.resource_id == "DEVICE_X"
    assert lock.bolt_state == LockBoltState.LOCKED
    # Fallback values when description / battery / located traits absent
    assert lock.serial_number == "DEVICE_X"
    assert lock.name == "Lock"
    assert lock.battery_level is None
    assert lock.location is None


def test_extract_lock_state_unlocked():
    bolt = _make_bolt_trait(
        locked_state=weave_security_pb2.BoltLockTrait.BoltLockedState.BOLT_LOCKED_STATE_UNLOCKED,
    )
    lock = _extract_lock_state(
        "DEVICE_X", {weave_security_pb2.BoltLockTrait.DESCRIPTOR.full_name: bolt}
    )
    assert lock.bolt_state == LockBoltState.UNLOCKED


def test_extract_lock_state_actuator_overrides_locked_state():
    # Actuator transitioning takes priority over the steady locked state
    bolt = _make_bolt_trait(
        locked_state=weave_security_pb2.BoltLockTrait.BoltLockedState.BOLT_LOCKED_STATE_UNLOCKED,
        actuator_state=weave_security_pb2.BoltLockTrait.BoltActuatorState.BOLT_ACTUATOR_STATE_LOCKING,
    )
    lock = _extract_lock_state(
        "DEVICE_X", {weave_security_pb2.BoltLockTrait.DESCRIPTOR.full_name: bolt}
    )
    assert lock.bolt_state == LockBoltState.LOCKING


def test_extract_lock_state_jammed():
    bolt = _make_bolt_trait(
        actuator_state=weave_security_pb2.BoltLockTrait.BoltActuatorState.BOLT_ACTUATOR_STATE_JAMMED_LOCKING,
    )
    lock = _extract_lock_state(
        "DEVICE_X", {weave_security_pb2.BoltLockTrait.DESCRIPTOR.full_name: bolt}
    )
    assert lock.bolt_state == LockBoltState.JAMMED


def test_extract_lock_state_no_bolt_trait_returns_none():
    # Only an unrelated trait — not a lock
    identity = weave_description_pb2.DeviceIdentityTrait()
    traits = {weave_description_pb2.DeviceIdentityTrait.DESCRIPTOR.full_name: identity}
    assert _extract_lock_state("DEVICE_X", traits) is None


def test_extract_lock_state_with_description_and_battery():
    bolt = _make_bolt_trait()
    identity = weave_description_pb2.DeviceIdentityTrait()
    identity.serialNumber = "ABC123"
    identity.softwareVersion = "1.2-7"
    label = weave_description_pb2.LabelSettingsTrait()
    label.label = "Front Door"
    battery = weave_power_pb2.BatteryPowerSourceTrait()
    battery.remaining.remainingPercent.value = 0.85

    lock = _extract_lock_state(
        "DEVICE_X",
        {
            weave_security_pb2.BoltLockTrait.DESCRIPTOR.full_name: bolt,
            weave_description_pb2.DeviceIdentityTrait.DESCRIPTOR.full_name: identity,
            weave_description_pb2.LabelSettingsTrait.DESCRIPTOR.full_name: label,
            weave_power_pb2.BatteryPowerSourceTrait.DESCRIPTOR.full_name: battery,
        },
    )
    assert lock.serial_number == "ABC123"
    assert lock.software_version == "1.2-7"
    assert lock.name == "Front Door"
    assert lock.battery_level == pytest.approx(85.0)


# -- _compose_lock_device_name ----------------------------------------------


@pytest.mark.parametrize(
    ("location", "name", "expected"),
    [
        ("Front Door", "Lock", "Front Door Lock"),
        (
            "Front Door",
            "Front Door Lock",
            "Front Door Lock",
        ),  # location already in name
        ("Hallway", "My Custom Name", "Hallway My Custom Name"),
        (None, "My Custom Name", "My Custom Name"),
        (None, "Lock", "Nest x Yale Lock"),  # bare fallback
        ("", "", "Nest x Yale Lock"),
    ],
)
def test_compose_lock_device_name(location, name, expected):
    assert _compose_lock_device_name(location, name) == expected


# -- _resolve_lock_location -------------------------------------------------


def _make_located_settings(
    *,
    where_label_literal: str | None = None,
    where_annotation_rid: str | None = None,
    fixture_annotation_rid: str | None = None,
) -> nest_located_pb2.DeviceLocatedSettingsTrait:
    trait = nest_located_pb2.DeviceLocatedSettingsTrait()
    if where_label_literal is not None:
        trait.whereLabel.literal = where_label_literal
    if where_annotation_rid is not None:
        trait.whereAnnotationRid.resourceId = where_annotation_rid
    if fixture_annotation_rid is not None:
        trait.fixtureAnnotationRid.resourceId = fixture_annotation_rid
    return trait


def test_resolve_lock_location_prefers_literal():
    settings = _make_located_settings(
        where_label_literal="Front Door",
        where_annotation_rid="ANNOTATION_AAAA",
    )
    traits = {
        nest_located_pb2.DeviceLocatedSettingsTrait.DESCRIPTOR.full_name: settings
    }
    # Even though the map has an entry, the literal wins.
    assert (
        _resolve_lock_location(traits, {"ANNOTATION_AAAA": "Other Room"})
        == "Front Door"
    )


def test_resolve_lock_location_falls_back_to_wheres_map():
    settings = _make_located_settings(where_annotation_rid="ANNOTATION_AAAA")
    traits = {
        nest_located_pb2.DeviceLocatedSettingsTrait.DESCRIPTOR.full_name: settings
    }
    assert (
        _resolve_lock_location(traits, {"ANNOTATION_AAAA": "Back Door"}) == "Back Door"
    )


def test_resolve_lock_location_returns_none_when_unresolved():
    settings = _make_located_settings(where_annotation_rid="ANNOTATION_UNKNOWN")
    traits = {
        nest_located_pb2.DeviceLocatedSettingsTrait.DESCRIPTOR.full_name: settings
    }
    assert _resolve_lock_location(traits, {}) is None


def test_resolve_lock_location_no_trait_returns_none():
    assert _resolve_lock_location({}, {"ANNOTATION_AAAA": "Front Door"}) is None


def test_extract_lock_state_sets_location_from_wheres_map():
    bolt = _make_bolt_trait()
    settings = _make_located_settings(where_annotation_rid="ANNOTATION_FRONT")
    lock = _extract_lock_state(
        "DEVICE_X",
        {
            weave_security_pb2.BoltLockTrait.DESCRIPTOR.full_name: bolt,
            nest_located_pb2.DeviceLocatedSettingsTrait.DESCRIPTOR.full_name: settings,
        },
        wheres_map={"ANNOTATION_FRONT": "Front Door"},
    )
    assert lock is not None
    assert lock.location == "Front Door"


def test_extract_lock_state_ignores_empty_identity_fields():
    # proto3 scalars have no presence: an identity trait that omits these
    # yields "", which must not become the device identifier or sw_version.
    bolt = _make_bolt_trait()
    identity = weave_description_pb2.DeviceIdentityTrait()

    lock = _extract_lock_state(
        "DEVICE_X",
        {
            weave_security_pb2.BoltLockTrait.DESCRIPTOR.full_name: bolt,
            weave_description_pb2.DeviceIdentityTrait.DESCRIPTOR.full_name: identity,
        },
    )
    assert lock.serial_number == "DEVICE_X"
    assert lock.software_version is None


# -- Fake aiohttp session ----------------------------------------------------


class _FakeNestSession:
    access_token = "session-token-xyz"


class _FakeEnv:
    # Matches the real NestEnvironment.host, which already carries the scheme.
    host = "https://home.nest.com"


class _FakeContent:
    """Stand-in for `ClientResponse.content`."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def iter_chunked(self, _size: int):
        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"",
        text: str = "",
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status = status
        self.content = _FakeContent(chunks or [])
        self._body = body
        self._text = text

    @property
    def ok(self) -> bool:
        return self.status < 400

    async def read(self) -> bytes:
        return self._body

    async def text(self) -> str:
        return self._text

    def raise_for_status(self) -> None:
        if not self.ok:
            raise ClientResponseError(None, (), status=self.status)


class _FakePostContext:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *_exc) -> bool:
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse | None = None) -> None:
        self.response = response or _FakeResponse()
        self.calls: list[dict] = []

    def post(self, url, *, data=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "data": data, "headers": headers, "timeout": timeout}
        )
        return _FakePostContext(self.response)


class _FakeNestClient:
    def __init__(self, response: _FakeResponse | None = None) -> None:
        self.session = _FakeSession(response)
        self.nest_session = _FakeNestSession()
        self.environment = _FakeEnv()


def _make_client(response: _FakeResponse | None = None) -> GrpcLockClient:
    return GrpcLockClient(_FakeNestClient(response))  # type: ignore[arg-type]


# -- headers -----------------------------------------------------------------


def test_headers_do_not_double_the_scheme():
    headers = _make_client()._headers()
    assert headers["Referer"] == "https://home.nest.com/"
    assert headers["Origin"] == "https://home.nest.com"
    assert "https://https://" not in headers["Referer"]
    assert "https://https://" not in headers["Origin"]


def test_headers_use_the_protobuf_user_agent():
    headers = _make_client()._headers()
    assert headers["User-Agent"] == PROTOBUF_USER_AGENT
    assert headers["Authorization"] == "Basic session-token-xyz"


def test_headers_without_a_session_raise_auth_exception():
    client = _make_client()
    client._nest_client.nest_session = None
    with pytest.raises(NestLockAuthException):
        client._headers()


# -- GrpcLockClient.send_lock_command ----------------------------------------


def _ok_send_command_response() -> bytes:
    return v1_pb2.SendCommandResponse().SerializeToString()


def _decode_send_command(body: bytes) -> v1_pb2.SendCommandRequest:
    """Helper: parse a serialized SendCommandRequest back into protobuf."""
    req = v1_pb2.SendCommandRequest()
    req.ParseFromString(body)
    return req


async def _send(resource_id: str, lock: bool) -> tuple[GrpcLockClient, dict]:
    """Run the real send_lock_command and return the client plus its POST call."""
    client = _make_client(_FakeResponse(body=_ok_send_command_response()))
    await client.send_lock_command(resource_id, lock=lock)
    return client, client._nest_client.session.calls[0]


async def test_send_lock_command_posts_to_the_send_command_endpoint():
    _, call = await _send("DEVICE_TEST_42", lock=True)
    assert call["url"] == (
        "https://grpc-web.production.nest.com"
        "/nestlabs.gateway.v1.ResourceApi/SendCommand"
    )


async def test_send_lock_command_targets_correct_resource():
    _, call = await _send("DEVICE_TEST_42", lock=True)
    req = _decode_send_command(call["data"])
    assert req.resourceRequest.resourceId == "DEVICE_TEST_42"
    assert req.resourceRequest.requestId  # a uuid is generated per call
    assert len(req.resourceCommands) == 1
    assert req.resourceCommands[0].traitLabel == "bolt_lock"


async def test_send_lock_command_lock_vs_unlock():
    _, lock_call = await _send("X", lock=True)
    _, unlock_call = await _send("X", lock=False)

    lock_change = weave_security_pb2.BoltLockTrait.BoltLockChangeRequest()
    _decode_send_command(lock_call["data"]).resourceCommands[0].command.Unpack(
        lock_change
    )
    unlock_change = weave_security_pb2.BoltLockTrait.BoltLockChangeRequest()
    _decode_send_command(unlock_call["data"]).resourceCommands[0].command.Unpack(
        unlock_change
    )

    assert (
        lock_change.state
        == weave_security_pb2.BoltLockTrait.BoltState.BOLT_STATE_EXTENDED
    )
    assert (
        unlock_change.state
        == weave_security_pb2.BoltLockTrait.BoltState.BOLT_STATE_RETRACTED
    )


async def test_send_lock_command_actor_is_remote_user_explicit():
    _, call = await _send("X", lock=True)
    change = weave_security_pb2.BoltLockTrait.BoltLockChangeRequest()
    _decode_send_command(call["data"]).resourceCommands[0].command.Unpack(change)
    assert (
        change.boltLockActor.method
        == weave_security_pb2.BoltLockTrait.BoltLockActorMethod.BOLT_LOCK_ACTOR_METHOD_REMOTE_USER_EXPLICIT
    )


@pytest.mark.parametrize("status", [401, 403])
async def test_send_lock_command_raises_auth_exception(status):
    client = _make_client(_FakeResponse(status=status, text="nope"))
    with pytest.raises(NestLockAuthException):
        await client.send_lock_command("X", lock=True)


async def test_send_lock_command_raises_command_exception_on_http_error():
    client = _make_client(_FakeResponse(status=500, text="boom"))
    with pytest.raises(NestLockCommandException):
        await client.send_lock_command("X", lock=True)


async def test_send_lock_command_raises_when_gateway_rejects_the_command():
    resp = v1_pb2.SendCommandResponse()
    resp.status.code = 7
    resp.status.message = "permission denied"
    client = _make_client(_FakeResponse(body=resp.SerializeToString()))

    with pytest.raises(NestLockCommandException, match="code=7"):
        await client.send_lock_command("X", lock=True)


# -- observe frame parsing ---------------------------------------------------


def _make_inner(
    *,
    trait_states: list[tuple[str, object]] | None = None,
    resource_metas: list[tuple[str, list[str]]] | None = None,
    metas_continue: bool | None = None,
) -> v2_pb2.ObserveResponse.ObserveResponse:
    """Build one inner ObserveResponse.

    `trait_states` is [(resource_id, trait_proto)], `resource_metas` is
    [(resource_id, [trait_type_name, ...])].
    """
    inner = v2_pb2.ObserveResponse.ObserveResponse()
    for resource_id, trait in trait_states or []:
        state = inner.traitStates.add()
        state.traitId.resourceId = resource_id
        state.patch.values.Pack(trait, type_url_prefix="type.nestlabs.com/")
    for resource_id, trait_types in resource_metas or []:
        meta = inner.resourceMetas.add()
        meta.resourceId = resource_id
        for trait_type in trait_types:
            meta.traitMetas.add().type = trait_type
    if metas_continue is not None:
        inner.initialResourceMetasContinue = metas_continue
    return inner


def _build_frame(inner: v2_pb2.ObserveResponse.ObserveResponse) -> bytes:
    """Serialize one StreamBody-framed observe frame.

    A frame is exactly the wire encoding of an ObserveResponse holding a single
    inner response, tag and length prefix included — see the note in
    `_parse_observe_buffer` about the field-1 aliasing this relies on.
    """
    return v2_pb2.ObserveResponse(observeResponse=[inner]).SerializeToString()


def test_parse_observe_buffer_single_frame():
    inner = _make_inner(trait_states=[("DEVICE_X", _make_bolt_trait())])
    buffer = bytearray(_build_frame(inner))

    client = _make_client()
    assert client._parse_observe_buffer(buffer) == [{"DEVICE_X"}]
    assert buffer == bytearray()  # fully drained


def test_parse_observe_buffer_multiple_frames_in_one_read():
    buffer = bytearray(
        _build_frame(_make_inner(trait_states=[("DEVICE_A", _make_bolt_trait())]))
        + _build_frame(_make_inner(trait_states=[("DEVICE_B", _make_bolt_trait())]))
    )

    client = _make_client()
    assert client._parse_observe_buffer(buffer) == [{"DEVICE_A"}, {"DEVICE_B"}]
    assert buffer == bytearray()


def test_parse_observe_buffer_leaves_partial_frame_for_next_read():
    frame = _build_frame(_make_inner(trait_states=[("DEVICE_X", _make_bolt_trait())]))
    split = len(frame) // 2
    client = _make_client()

    buffer = bytearray(frame[:split])
    assert client._parse_observe_buffer(buffer) == []
    assert bytes(buffer) == frame[:split]  # untouched, waiting for the rest

    buffer.extend(frame[split:])
    assert client._parse_observe_buffer(buffer) == [{"DEVICE_X"}]
    assert buffer == bytearray()


def test_parse_observe_buffer_handles_a_split_mid_varint():
    # A frame whose length prefix needs two varint bytes, delivered one byte at
    # a time, so the buffer is repeatedly cut in the middle of the varint.
    label = weave_description_pb2.LabelSettingsTrait()
    label.label = "L" * 300  # forces a multi-byte length varint
    frame = _build_frame(
        _make_inner(
            trait_states=[("DEVICE_X", _make_bolt_trait()), ("DEVICE_X", label)]
        )
    )
    assert frame[1] & 0x80, "expected a multi-byte length varint for this fixture"

    client = _make_client()
    buffer = bytearray()
    results: list[set[str]] = []
    for byte in frame:
        buffer.append(byte)
        results.extend(client._parse_observe_buffer(buffer))

    assert results == [{"DEVICE_X"}]
    assert buffer == bytearray()


def test_parse_observe_buffer_resets_on_unexpected_wire_type():
    client = _make_client()
    buffer = bytearray(b"\x08\x01")  # field 1, wire type 0 (varint)
    assert client._parse_observe_buffer(buffer) == []
    assert buffer == bytearray()  # cleared, since the stream is out of sync


# -- _ingest_observe_response ------------------------------------------------


def test_ingest_caches_traits_and_reports_touched_resources():
    client = _make_client()
    inner = _make_inner(
        trait_states=[
            ("DEVICE_X", _make_bolt_trait()),
            ("DEVICE_Y", _make_bolt_trait()),
        ]
    )
    assert client._ingest_observe_response(inner) == {"DEVICE_X", "DEVICE_Y"}
    assert (
        weave_security_pb2.BoltLockTrait.DESCRIPTOR.full_name
        in client._trait_cache["DEVICE_X"]
    )


def test_ingest_skips_traits_outside_the_subscribed_set():
    client = _make_client()
    inner = v2_pb2.ObserveResponse.ObserveResponse()
    state = inner.traitStates.add()
    state.traitId.resourceId = "DEVICE_X"
    state.patch.values.type_url = "type.nestlabs.com/some.unrelated.Trait"

    assert client._ingest_observe_response(inner) == set()
    assert client._trait_cache == {}


def test_ingest_bolt_lock_trait_marks_locks_present():
    client = _make_client()
    assert client._locks_present is None
    client._ingest_observe_response(
        _make_inner(trait_states=[("DEVICE_X", _make_bolt_trait())])
    )
    assert client._locks_present is True


def test_ingest_bolt_lock_resource_meta_marks_locks_present():
    client = _make_client()
    client._ingest_observe_response(
        _make_inner(
            resource_metas=[("DEVICE_X", ["weave.trait.security.BoltLockTrait"])],
            metas_continue=False,
        )
    )
    assert client._locks_present is True
    assert client._seen_resource_metas is True
    assert client._initial_metas_continue is False


def test_ingest_tracks_the_initial_meta_continuation_flag():
    client = _make_client()
    client._ingest_observe_response(
        _make_inner(resource_metas=[("DEVICE_X", [])], metas_continue=True)
    )
    assert client._seen_resource_metas is True
    assert client._initial_metas_continue is True


def test_ingest_annotation_change_re_emits_every_cached_lock():
    client = _make_client()
    client._ingest_observe_response(
        _make_inner(trait_states=[("DEVICE_X", _make_bolt_trait())])
    )

    ann = nest_located_pb2.LocatedAnnotationsTrait()
    item = ann.predefinedWheres[1]
    item.whereId.resourceId = "ANNOTATION_FRONT"
    item.label.literal = "Front Door"

    # The annotation lives on the structure resource, but every known lock is
    # re-emitted so the new label propagates.
    touched = client._ingest_observe_response(
        _make_inner(trait_states=[("STRUCTURE_1", ann)])
    )
    assert touched == {"DEVICE_X"}
    assert client._wheres_map == {"ANNOTATION_FRONT": "Front Door"}


# -- _rebuild_wheres_map -----------------------------------------------------


def test_rebuild_wheres_map_merges_predefined_and_custom_wheres():
    ann = nest_located_pb2.LocatedAnnotationsTrait()
    predefined = ann.predefinedWheres[1]
    predefined.whereId.resourceId = "ANNOTATION_FRONT"
    predefined.label.literal = "Front Door"
    custom = ann.customWheres[2]
    custom.whereId.resourceId = "ANNOTATION_SIDE"
    custom.label.literal = "Side Gate"

    custom_trait = nest_located_pb2.CustomLocatedAnnotationsTrait()
    where = custom_trait.wheresList[3]
    where.whereId.resourceId = "ANNOTATION_SHED"
    where.label.literal = "Shed"
    fixture = custom_trait.fixturesList[4]
    fixture.fixtureId.resourceId = "FIXTURE_BACK"
    fixture.label.literal = "Back Door"

    client = _make_client()
    client._trait_cache = {
        "STRUCTURE_1": {
            nest_located_pb2.LocatedAnnotationsTrait.DESCRIPTOR.full_name: ann,
            nest_located_pb2.CustomLocatedAnnotationsTrait.DESCRIPTOR.full_name: (
                custom_trait
            ),
        }
    }
    client._rebuild_wheres_map()

    assert client._wheres_map == {
        "ANNOTATION_FRONT": "Front Door",
        "ANNOTATION_SIDE": "Side Gate",
        "ANNOTATION_SHED": "Shed",
        "FIXTURE_BACK": "Back Door",
    }


def test_rebuild_wheres_map_skips_incomplete_entries():
    ann = nest_located_pb2.LocatedAnnotationsTrait()
    ann.predefinedWheres[1].whereId.resourceId = "ANNOTATION_NO_LABEL"

    client = _make_client()
    client._trait_cache = {
        "STRUCTURE_1": {
            nest_located_pb2.LocatedAnnotationsTrait.DESCRIPTOR.full_name: ann
        }
    }
    client._rebuild_wheres_map()
    assert client._wheres_map == {}


# -- _observe_once and lock presence -----------------------------------------


async def test_observe_once_yields_lock_states():
    frame = _build_frame(_make_inner(trait_states=[("DEVICE_X", _make_bolt_trait())]))
    client = _make_client(_FakeResponse(chunks=[frame]))

    batches = [batch async for batch in client._observe_once()]
    assert len(batches) == 1
    assert batches[0]["DEVICE_X"].bolt_state == LockBoltState.LOCKED


async def test_observe_once_uses_a_read_timeout_not_a_total_timeout():
    client = _make_client(_FakeResponse(chunks=[]))
    _ = [batch async for batch in client._observe_once()]

    timeout = client._nest_client.session.calls[0]["timeout"]
    # A total timeout would tear down a healthy idle stream on a fixed cycle.
    assert timeout.total is None
    assert timeout.sock_read == 300


@pytest.mark.parametrize("status", [401, 403])
async def test_observe_once_raises_auth_exception(status):
    client = _make_client(_FakeResponse(status=status))
    with pytest.raises(NestLockAuthException):
        _ = [batch async for batch in client._observe_once()]


async def test_observe_once_concludes_no_locks_once_enumeration_settles():
    frame = _build_frame(
        _make_inner(
            resource_metas=[
                ("DEVICE_X", ["weave.trait.description.DeviceIdentityTrait"])
            ],
            metas_continue=False,
        )
    )
    client = _make_client(_FakeResponse(chunks=[frame]))
    client._first_observe_at = -1000.0  # settle window already elapsed

    assert [batch async for batch in client._observe_once()] == []
    assert client._locks_present is False


async def test_observe_once_waits_out_the_settle_window_before_concluding():
    frame = _build_frame(
        _make_inner(
            resource_metas=[
                ("DEVICE_X", ["weave.trait.description.DeviceIdentityTrait"])
            ],
            metas_continue=False,
        )
    )
    client = _make_client(_FakeResponse(chunks=[frame]))

    assert [batch async for batch in client._observe_once()] == []
    # Enumeration looks empty, but the stream only just opened, so no verdict.
    assert client._locks_present is None


async def test_observe_once_does_not_conclude_while_metas_still_arriving():
    frame = _build_frame(
        _make_inner(resource_metas=[("DEVICE_X", [])], metas_continue=True)
    )
    client = _make_client(_FakeResponse(chunks=[frame]))
    client._first_observe_at = -1000.0

    assert [batch async for batch in client._observe_once()] == []
    assert client._locks_present is None


async def test_observe_once_never_concludes_no_locks_when_a_lock_exists():
    frame = _build_frame(
        _make_inner(
            trait_states=[("DEVICE_X", _make_bolt_trait())],
            resource_metas=[("DEVICE_X", ["weave.trait.security.BoltLockTrait"])],
            metas_continue=False,
        )
    )
    client = _make_client(_FakeResponse(chunks=[frame]))
    client._first_observe_at = -1000.0

    batches = [batch async for batch in client._observe_once()]
    assert len(batches) == 1
    assert client._locks_present is True


# -- observe_locks reconnect loop --------------------------------------------


class _StopLoop(BaseException):
    """Sentinel used to break out of the endless reconnect loop under test.

    Deliberately a BaseException so `observe_locks`'s broad `except Exception`
    cannot swallow it.
    """


@pytest.fixture
def recorded_sleeps(monkeypatch):
    """Replace asyncio.sleep with a recorder that ends the loop after 4 calls."""
    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 4:
            raise _StopLoop

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return sleeps


def _observe_once_stub(monkeypatch, behaviour):
    async def _stub(self):
        await behaviour(self)
        return
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(GrpcLockClient, "_observe_once", _stub)


async def test_observe_locks_backs_off_when_the_stream_ends_cleanly(
    monkeypatch, recorded_sleeps
):
    # A stream that ends immediately must not reconnect once a second forever.
    async def _clean_end(_self):
        return

    _observe_once_stub(monkeypatch, _clean_end)

    client = _make_client()
    with pytest.raises(_StopLoop):
        async for _ in client.observe_locks():
            pass

    assert recorded_sleeps == [1.0, 2.0, 4.0, 8.0]


async def test_observe_locks_backs_off_on_errors(monkeypatch, recorded_sleeps):
    async def _boom(_self):
        raise ClientError("connection reset")

    _observe_once_stub(monkeypatch, _boom)

    client = _make_client()
    with pytest.raises(_StopLoop):
        async for _ in client.observe_locks():
            pass

    assert recorded_sleeps == [1.0, 2.0, 4.0, 8.0]


async def test_observe_locks_caps_the_backoff(monkeypatch):
    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 12:
            raise _StopLoop

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    async def _boom(_self):
        raise ClientError("connection reset")

    _observe_once_stub(monkeypatch, _boom)

    client = _make_client()
    with pytest.raises(_StopLoop):
        async for _ in client.observe_locks():
            pass

    assert max(sleeps) == 60.0
    assert sleeps[-1] == 60.0


async def test_observe_locks_propagates_auth_failures_instead_of_retrying(
    monkeypatch, recorded_sleeps
):
    async def _rejected(_self):
        raise NestLockAuthException("Observe rejected with HTTP 401")

    _observe_once_stub(monkeypatch, _rejected)

    client = _make_client()
    with pytest.raises(NestLockAuthException):
        async for _ in client.observe_locks():
            pass

    # Retrying a rejected token never recovers, so it must not back off at all.
    assert recorded_sleeps == []


async def test_observe_locks_stops_when_the_account_has_no_locks(
    monkeypatch, recorded_sleeps
):
    async def _no_locks(client):
        client._locks_present = False

    _observe_once_stub(monkeypatch, _no_locks)

    client = _make_client()
    assert [batch async for batch in client.observe_locks()] == []
    assert recorded_sleeps == []


async def test_observe_locks_reaches_the_verdict_after_the_stream_ends(
    monkeypatch, recorded_sleeps
):
    # The stream ends right after the enumeration, so the per-chunk check never
    # had a chance to run. The exit check must still reach the verdict.
    async def _enumerated_then_ended(client):
        client._seen_resource_metas = True
        client._initial_metas_continue = False
        client._first_observe_at = -1000.0

    _observe_once_stub(monkeypatch, _enumerated_then_ended)

    client = _make_client()
    assert [batch async for batch in client.observe_locks()] == []
    assert client._locks_present is False
    assert recorded_sleeps == []


async def test_observe_locks_stays_quiet_about_errors_until_a_lock_is_known(
    monkeypatch, recorded_sleeps, caplog
):
    async def _boom(_self):
        raise ClientError("connection reset")

    _observe_once_stub(monkeypatch, _boom)

    client = _make_client()
    with pytest.raises(_StopLoop):
        async for _ in client.observe_locks():
            pass

    # Most accounts have no lock; their stream errors must not warn.
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_observe_locks_warns_about_errors_once_a_lock_is_known(
    monkeypatch, recorded_sleeps, caplog
):
    async def _boom(client):
        client._locks_present = True
        raise ClientError("connection reset")

    _observe_once_stub(monkeypatch, _boom)

    client = _make_client()
    with pytest.raises(_StopLoop):
        async for _ in client.observe_locks():
            pass

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings
    # %r, not %s: a bare TimeoutError renders as an empty string under %s.
    assert "ClientError" in warnings[0].getMessage()


async def test_observe_locks_resets_the_backoff_after_a_successful_batch(
    monkeypatch, recorded_sleeps
):
    calls = {"n": 0}

    async def _stub(self):
        calls["n"] += 1
        if calls["n"] % 2:
            yield {"DEVICE_X": "state"}  # a batch arrived: backoff resets

    monkeypatch.setattr(GrpcLockClient, "_observe_once", _stub)

    client = _make_client()
    with pytest.raises(_StopLoop):
        async for _ in client.observe_locks():
            pass

    # Attempts alternate yield / no-yield. Each yielding attempt resets the
    # delay to the initial 1.0s, so it never compounds past one doubling.
    assert recorded_sleeps == [1.0, 2.0, 1.0, 2.0]
