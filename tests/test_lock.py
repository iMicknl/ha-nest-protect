"""Tests for the Nest x Yale lock entity."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect.const import DOMAIN
from custom_components.nest_protect.lock import NestLockEntity
from custom_components.nest_protect.pynest.lock_models import LockBoltState, LockState

SERIAL = "ABC123"


def _lock_state(
    *,
    location: str | None = None,
    software_version: str | None = None,
    name: str = "Lock",
) -> LockState:
    return LockState(
        resource_id="DEVICE_X",
        name=name,
        serial_number=SERIAL,
        bolt_state=LockBoltState.LOCKED,
        software_version=software_version,
        battery_level=None,
        location=location,
    )


@pytest.fixture
async def added_lock(hass):
    """A NestLockEntity whose device already exists in the registry.

    Mirrors what the entity platform does at add time: it reads `device_info`
    once and creates the device from it. Reproducing that here is the point —
    `device_info` is never re-read afterwards, so anything arriving later has
    to be written to the registry explicitly.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    initial = _lock_state()
    entity = NestLockEntity(MagicMock(), initial)

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        **{
            key: value
            for key, value in entity.device_info.items()
            if key in {"identifiers", "name", "manufacturer", "model", "sw_version"}
        },
    )

    entity.hass = hass
    entity.entity_id = "lock.front_door"
    entity.device_entry = device
    return entity, device


async def test_device_starts_with_the_fallback_name(added_lock):
    _, device = added_lock
    assert device.name == "Nest x Yale Lock"
    assert device.sw_version is None


async def test_late_location_updates_the_device_registry(hass, added_lock):
    """The defect: a room label that resolves later must reach the registry.

    Asserting on `entity.device_info` instead would pass even when the registry
    was never touched, which is what makes this easy to miss.
    """
    entity, device = added_lock

    with patch.object(NestLockEntity, "async_write_ha_state"):
        entity._handle_update(
            _lock_state(location="Front Door", software_version="1.2-7")
        )

    updated = dr.async_get(hass).async_get(device.id)
    assert updated.name == "Front Door Lock"
    assert updated.sw_version == "1.2-7"


async def test_unchanged_location_leaves_the_registry_alone(hass, added_lock):
    entity, device = added_lock

    with (
        patch.object(NestLockEntity, "async_write_ha_state"),
        patch.object(
            dr.DeviceRegistry, "async_update_device", autospec=True
        ) as update_device,
    ):
        # Same location and sw_version, only the bolt moved.
        entity._handle_update(_lock_state())

    update_device.assert_not_called()
    assert dr.async_get(hass).async_get(device.id).name == "Nest x Yale Lock"


async def test_update_without_a_device_entry_is_a_no_op(hass, added_lock):
    """An update arriving before the platform assigns device_entry must not raise."""
    entity, _ = added_lock
    entity.device_entry = None

    with patch.object(NestLockEntity, "async_write_ha_state"):
        entity._handle_update(_lock_state(location="Front Door"))

    assert entity._lock_state.location == "Front Door"
