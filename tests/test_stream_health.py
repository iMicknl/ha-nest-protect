"""Tests for subscriber stream health tracking."""

import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityDescription
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.nest_protect import _async_subscribe_for_data
from custom_components.nest_protect.const import DOMAIN, STREAM_STALE_AFTER_SECONDS
from custom_components.nest_protect.entity import NestEntity
from custom_components.nest_protect.pynest.exceptions import (
    EmptyResponseException,
    NestServiceException,
    NotAuthenticatedException,
)
from custom_components.nest_protect.pynest.models import Bucket
from custom_components.nest_protect.stream_health import StreamHealth

from .test_init import _make_subscribe_data, _make_subscriber_entry_data

STALE = timedelta(seconds=STREAM_STALE_AFTER_SECONDS)


@pytest.fixture
async def health(hass):
    """Return a started stream health tracker that is stopped afterwards."""
    tracker = StreamHealth(hass, "entry")
    tracker.async_record_response()
    yield tracker
    tracker.async_stop()


async def _advance(hass, freezer, delta: timedelta) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def test_threshold_outlasts_a_silent_long_poll():
    """A healthy long-poll may stay silent for its full 600 s timeout."""
    assert STREAM_STALE_AFTER_SECONDS > 600


async def test_stays_available_within_grace_period(hass, freezer, health):
    """Quiet periods shorter than the threshold are not an outage."""
    await _advance(hass, freezer, STALE - timedelta(seconds=1))

    assert health.available


async def test_becomes_unavailable_once_after_grace_period(
    hass, freezer, health, caplog
):
    """Missing responses mark the stream stale and notify listeners once."""
    signals = []
    async_dispatcher_connect(hass, health.signal, lambda: signals.append(True))

    with caplog.at_level(logging.INFO):
        await _advance(hass, freezer, STALE + timedelta(seconds=1))
        await _advance(hass, freezer, 3 * STALE)

    assert not health.available
    assert len(signals) == 1
    assert caplog.text.count("marking entities unavailable") == 1


async def test_response_resets_grace_period(hass, freezer, health):
    """Every response pushes the stale deadline out again."""
    await _advance(hass, freezer, STALE * 0.75)
    health.async_record_response()

    await _advance(hass, freezer, STALE * 0.75)

    assert health.available


async def test_restored_once_after_outage(hass, freezer, health, caplog):
    """The first response after an outage restores and notifies once."""
    await _advance(hass, freezer, STALE + timedelta(seconds=1))

    signals = []
    async_dispatcher_connect(hass, health.signal, lambda: signals.append(True))

    with caplog.at_level(logging.INFO):
        health.async_record_response()
        health.async_record_response()
        await hass.async_block_till_done()

    assert health.available
    assert len(signals) == 1
    assert caplog.text.count("entities are available again") == 1


async def test_stop_cancels_pending_check(hass, freezer):
    """Unloading must not leave the stale timer behind."""
    tracker = StreamHealth(hass, "entry")
    tracker.async_record_response()
    tracker.async_stop()

    await _advance(hass, freezer, STALE * 2)

    assert tracker.available


def _build_entity(hass, health, cls=NestEntity) -> NestEntity:
    entity = cls(
        bucket=Bucket("topaz.1234", 1, 1, {}),
        description=EntityDescription(key="test"),
        areas={},
        client=MagicMock(),
    )
    entity.hass = hass
    entity.platform = MagicMock(config_entry=SimpleNamespace(entry_id="entry"))
    entity.async_write_ha_state = MagicMock()
    hass.data.setdefault(DOMAIN, {})["entry"] = SimpleNamespace(stream_health=health)
    return entity


async def test_entity_follows_stream_health(hass, freezer, health):
    """Entities go unavailable with the stream and write state on changes."""
    entity = _build_entity(hass, health)
    await entity.async_added_to_hass()
    assert entity.available

    await _advance(hass, freezer, STALE + timedelta(seconds=1))

    assert not entity.available
    entity.async_write_ha_state.assert_called_once()

    health.async_record_response()
    await hass.async_block_till_done()

    assert entity.available
    assert entity.async_write_ha_state.call_count == 2


async def test_entity_can_opt_out_of_stream_health(hass, freezer, health):
    """An entity reporting device connectivity stays available."""

    class ConnectivityEntity(NestEntity):
        _follows_stream_health = False

    entity = _build_entity(hass, health, ConnectivityEntity)
    await entity.async_added_to_hass()

    await _advance(hass, freezer, STALE + timedelta(seconds=1))

    assert entity.available
    entity.async_write_ha_state.assert_not_called()


def test_entity_not_added_is_available():
    """Entities built outside Home Assistant keep the default availability."""
    entity = NestEntity(
        bucket=Bucket("topaz.1234", 1, 1, {}),
        description=EntityDescription(key="test"),
        areas={},
        client=MagicMock(),
    )
    assert entity.available


@pytest.mark.parametrize(
    "outcome",
    [
        {"return_value": {"objects": []}},
        {"side_effect": TimeoutError()},
        {"side_effect": EmptyResponseException()},
    ],
    ids=["data", "long_poll_timeout", "empty_response"],
)
async def test_subscriber_records_response(hass, outcome):
    """Completed long-polls count as a healthy stream."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    client, sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock(**outcome)

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    health = hass.data[DOMAIN][entry.entry_id].stream_health
    health.async_record_response.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ServerDisconnectedError(),
        aiohttp.ClientOSError(),
        NestServiceException(),
        NotAuthenticatedException(),
    ],
)
async def test_subscriber_failure_does_not_record_response(hass, error):
    """Failed requests must let the stale deadline run out."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    client, sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock(side_effect=error)

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch.object(sm, "async_refresh_session", new_callable=AsyncMock),
        patch("custom_components.nest_protect.asyncio.sleep", new_callable=AsyncMock),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    health = hass.data[DOMAIN][entry.entry_id].stream_health
    health.async_record_response.assert_not_called()


async def test_session_refresh_timeout_does_not_record_response(hass):
    """A timeout before the subscribe request was sent proves nothing about Nest."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    client, sm = _make_subscriber_entry_data(hass, entry)
    client.subscribe_for_data = AsyncMock()

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch.object(
            sm, "ensure_session", new_callable=AsyncMock, side_effect=TimeoutError()
        ),
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    client.subscribe_for_data.assert_not_called()
    health = hass.data[DOMAIN][entry.entry_id].stream_health
    health.async_record_response.assert_not_called()
