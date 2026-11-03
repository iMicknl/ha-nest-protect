"""Tests for the Nest Protect switch platform."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers.entity import EntityCategory

from custom_components.nest_protect.pynest.models import Bucket
from custom_components.nest_protect.switch import (
    SWITCH_DESCRIPTIONS,
    NestProtectSwitch,
    async_setup_entry,
)

OBJECT_KEY = "topaz.1234"

# Pathlight fields as reported by a Protect set to "always on" (issue #70 dump)
TOPAZ = {
    "where_id": "where.hallway",
    "night_light_enable": True,
    "night_light_continuous": True,
    "night_light_brightness": 3,
}


def _bucket(value: dict) -> Bucket:
    return Bucket(
        object_key=OBJECT_KEY, object_revision=1, object_timestamp=1, value=value
    )


def _description(key: str):
    return next(d for d in SWITCH_DESCRIPTIONS if d.key == key)


def _switch(key: str, value: dict) -> NestProtectSwitch:
    entity = NestProtectSwitch(
        _bucket(value), _description(key), {}, MagicMock(), MagicMock()
    )
    entity._async_update_objects = AsyncMock(return_value={})
    return entity


def _written_value(entity: NestProtectSwitch) -> dict:
    (objects,) = entity._async_update_objects.await_args.args
    assert [obj["object_key"] for obj in objects] == [OBJECT_KEY]
    return objects[0]["value"]


async def test_always_on_switch_created_for_protects():
    entry = MagicMock()
    entry.runtime_data.devices = {OBJECT_KEY: _bucket(TOPAZ)}
    entry.runtime_data.areas = {}
    added = []

    await async_setup_entry(MagicMock(), entry, added.extend)

    keys = {entity.entity_description.key for entity in added}
    assert {"night_light_enable", "night_light_continuous"} <= keys


def test_always_on_is_a_config_entity():
    description = _description("night_light_continuous")
    assert description.translation_key == "night_light_continuous"
    assert description.entity_category is EntityCategory.CONFIG


@pytest.mark.parametrize("continuous", [True, False])
def test_always_on_state_follows_bucket(continuous):
    entity = _switch(
        "night_light_continuous", {**TOPAZ, "night_light_continuous": continuous}
    )
    assert entity.is_on is continuous


async def test_turning_on_always_on_also_enables_pathlight():
    entity = _switch(
        "night_light_continuous",
        {**TOPAZ, "night_light_enable": False, "night_light_continuous": False},
    )

    await entity.async_turn_on()

    assert _written_value(entity) == {
        "night_light_continuous": True,
        "night_light_enable": True,
    }


async def test_turning_off_always_on_keeps_pathlight_enabled():
    entity = _switch("night_light_continuous", TOPAZ)

    await entity.async_turn_off()

    assert _written_value(entity) == {"night_light_continuous": False}


@pytest.mark.parametrize(
    ("method", "expected"), [("async_turn_on", True), ("async_turn_off", False)]
)
async def test_pathlight_switch_writes_only_its_own_key(method, expected):
    entity = _switch("night_light_enable", TOPAZ)

    await getattr(entity, method)()

    assert _written_value(entity) == {"night_light_enable": expected}
