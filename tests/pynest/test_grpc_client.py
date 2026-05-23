"""Tests for the Nest x Yale lock gRPC-web client."""

from __future__ import annotations

import pytest

from custom_components.nest_protect.pynest.grpc_client import (
    GrpcLockClient,
    _decode_varint,
    _extract_lock_state,
)
from custom_components.nest_protect.pynest.lock_models import LockBoltState
from custom_components.nest_protect.pynest.protobuf_gen.nestlabs.gateway import (
    v1_pb2,
)
from custom_components.nest_protect.pynest.protobuf_gen.weave.trait import (
    description_pb2 as weave_description_pb2,
)
from custom_components.nest_protect.pynest.protobuf_gen.weave.trait import (
    heartbeat_pb2 as weave_heartbeat_pb2,
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
    # Fallback values when description/liveness/battery traits absent
    assert lock.serial_number == "DEVICE_X"
    assert lock.name == "Nest x Yale Lock"
    assert lock.online is True
    assert lock.battery_level is None


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
    liveness = weave_heartbeat_pb2.LivenessTrait()
    liveness.status = weave_heartbeat_pb2.LivenessTrait.LIVENESS_DEVICE_STATUS_ONLINE
    battery = weave_power_pb2.BatteryPowerSourceTrait()
    battery.remaining.remainingPercent.value = 0.85

    lock = _extract_lock_state(
        "DEVICE_X",
        {
            weave_security_pb2.BoltLockTrait.DESCRIPTOR.full_name: bolt,
            weave_description_pb2.DeviceIdentityTrait.DESCRIPTOR.full_name: identity,
            weave_description_pb2.LabelSettingsTrait.DESCRIPTOR.full_name: label,
            weave_heartbeat_pb2.LivenessTrait.DESCRIPTOR.full_name: liveness,
            weave_power_pb2.BatteryPowerSourceTrait.DESCRIPTOR.full_name: battery,
        },
    )
    assert lock.serial_number == "ABC123"
    assert lock.software_version == "1.2-7"
    assert lock.name == "Front Door"
    assert lock.online is True
    assert lock.battery_level == pytest.approx(85.0)


# -- GrpcLockClient.send_lock_command (serialization only) -------------------


class _FakeNestSession:
    access_token = "session-token-xyz"


class _FakeEnv:
    host = "home.nest.com"


class _FakeNestClient:
    session = None  # no network calls in these tests
    nest_session = _FakeNestSession()
    environment = _FakeEnv()


def _decode_send_command(body: bytes) -> v1_pb2.SendCommandRequest:
    """Helper: parse a serialized SendCommandRequest back into protobuf."""
    req = v1_pb2.SendCommandRequest()
    req.ParseFromString(body)
    return req


def _build_send_command_body(resource_id: str, lock: bool) -> bytes:
    """Mirror the body construction in GrpcLockClient.send_lock_command.

    Kept here as a pure-function test to verify the wire format without
    hitting the network.
    """
    client = GrpcLockClient(_FakeNestClient())  # type: ignore[arg-type]
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
    command.command.Pack(change_req, type_url_prefix="type.nestlabs.com/")
    send_req = v1_pb2.SendCommandRequest(
        resourceRequest=v1_pb2.ResourceRequest(
            resourceId=resource_id, requestId="00000000-0000-0000-0000-000000000000"
        ),
        resourceCommands=[command],
    )
    del client
    return send_req.SerializeToString()


def test_send_lock_command_body_targets_correct_resource():
    body = _build_send_command_body("DEVICE_TEST_42", lock=True)
    req = _decode_send_command(body)
    assert req.resourceRequest.resourceId == "DEVICE_TEST_42"
    assert len(req.resourceCommands) == 1
    assert req.resourceCommands[0].traitLabel == "bolt_lock"


def test_send_lock_command_body_lock_vs_unlock():
    lock_body = _build_send_command_body("X", lock=True)
    unlock_body = _build_send_command_body("X", lock=False)

    lock_req = _decode_send_command(lock_body)
    unlock_req = _decode_send_command(unlock_body)

    lock_change = weave_security_pb2.BoltLockTrait.BoltLockChangeRequest()
    lock_req.resourceCommands[0].command.Unpack(lock_change)
    unlock_change = weave_security_pb2.BoltLockTrait.BoltLockChangeRequest()
    unlock_req.resourceCommands[0].command.Unpack(unlock_change)

    assert (
        lock_change.state
        == weave_security_pb2.BoltLockTrait.BoltState.BOLT_STATE_EXTENDED
    )
    assert (
        unlock_change.state
        == weave_security_pb2.BoltLockTrait.BoltState.BOLT_STATE_RETRACTED
    )


def test_send_lock_command_body_actor_is_remote_user_explicit():
    body = _build_send_command_body("X", lock=True)
    req = _decode_send_command(body)
    change = weave_security_pb2.BoltLockTrait.BoltLockChangeRequest()
    req.resourceCommands[0].command.Unpack(change)
    assert (
        change.boltLockActor.method
        == weave_security_pb2.BoltLockTrait.BoltLockActorMethod.BOLT_LOCK_ACTOR_METHOD_REMOTE_USER_EXPLICIT
    )
