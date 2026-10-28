"""Tests for renaming entity IDs."""

from unittest.mock import patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import Entity

from custom_components.nest_protect.binary_sensor import (
    NestProtectBinarySensor,
    NestProtectConnectivitySensor,
)
from custom_components.nest_protect.const import DOMAIN
from custom_components.nest_protect.entity import NestEntity
from custom_components.nest_protect.lock import NestLockBatterySensor, NestLockEntity
from custom_components.nest_protect.pynest.models import Bucket
from custom_components.nest_protect.select import NestProtectSelect
from custom_components.nest_protect.sensor import NestHomeSensor, NestProtectSensor
from custom_components.nest_protect.switch import NestProtectSwitch

from .test_device_discovery import _make_entry, _setup, _setup_data

# Older HA always removes and re-adds an entity on rename; the hook is unused there.
pytestmark = pytest.mark.skipif(
    not hasattr(Entity, "async_entity_id_changed"),
    reason="In-place entity_id changes need HA 2026.11",
)


@pytest.mark.parametrize(
    "entity_class",
    [
        NestProtectBinarySensor,
        NestProtectConnectivitySensor,
        NestLockEntity,
        NestLockBatterySensor,
        NestProtectSelect,
        NestHomeSensor,
        NestProtectSensor,
        NestProtectSwitch,
    ],
)
def test_entity_id_change_keeps_entity_in_place(entity_class):
    """Renaming an entity_id doesn't remove and re-add the entity.

    HA only does this for custom entities that override an add or remove hook
    without async_entity_id_changed. The fallback, and this check, go away in
    HA 2027.11.
    """
    from homeassistant.helpers.entity import _entity_class_requires_readd

    assert not _entity_class_requires_readd(entity_class)


async def test_renamed_entity_keeps_receiving_updates(hass):
    """A renamed Protect entity stays subscribed to its bucket."""
    entry = _make_entry(hass)
    await _setup(hass, entry, _setup_data())
    entity_registry = er.async_get(hass)
    old_entity_id = entity_registry.async_get_entity_id(
        "binary_sensor", DOMAIN, "topaz.A-smoke_status"
    )
    assert hass.states.get(old_entity_id).state == STATE_OFF

    with patch.object(
        NestEntity, "async_added_to_hass", autospec=True
    ) as added_to_hass:
        entity_registry.async_update_entity(
            old_entity_id, new_entity_id="binary_sensor.renamed_smoke"
        )
        await hass.async_block_till_done()

    added_to_hass.assert_not_called()
    assert hass.states.get(old_entity_id) is None
    assert hass.states.get("binary_sensor.renamed_smoke").state == STATE_OFF

    async_dispatcher_send(
        hass,
        "topaz.A",
        Bucket(
            object_key="topaz.A",
            object_revision=2,
            object_timestamp=2,
            value={"serial_number": "SERIAL_A", "smoke_status": 2},
        ),
    )
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.renamed_smoke").state == STATE_ON

    await hass.config_entries.async_unload(entry.entry_id)
