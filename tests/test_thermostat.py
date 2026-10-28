"""Tests for the read-only Nest thermostat sensors."""

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect import (
    _dispatch_states,
    async_remove_config_entry_device,
)
from custom_components.nest_protect.const import DOMAIN
from custom_components.nest_protect.diagnostics import async_get_device_diagnostics
from custom_components.nest_protect.pynest.models import FirstDataAPIResponse
from custom_components.nest_protect.pynest.thermostat_models import (
    TemperatureSource,
    ThermostatState,
)
from custom_components.nest_protect.thermostat import (
    thermostat_discovery_signal,
    thermostat_signal,
)

from .conftest import COOKIES, ISSUE_TOKEN

SERIAL = "T123"

THERMOSTAT = ThermostatState(
    resource_id="DEVICE_T",
    serial_number=SERIAL,
    model="Learning Thermostat (3rd gen)",
    software_version="6.2",
    location="Living Room",
    backplate_temperature=21.46,
    current_temperature=19.0,
    current_humidity=48.0,
    temperature_source=TemperatureSource.BACKPLATE,
)


def _bucket(object_key: str, value: dict) -> dict:
    return {
        "object_key": object_key,
        "object_revision": 1,
        "object_timestamp": 1,
        "value": value,
    }


ACCOUNT_BUCKETS = [
    _bucket("where.home-1", {"wheres": [{"where_id": "w-1", "name": "Bedroom"}]}),
    _bucket("kryptonite.S1", {"description": "Nursery", "where_id": "w-1"}),
    _bucket("kryptonite.S2", {"where_id": "w-1"}),
]


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.nest_protect.NestSessionManager.async_setup",
            return_value=FirstDataAPIResponse(
                weather_for_structures={},
                service_urls={"urls": {"transport_url": "https://t.example.com"}},
                _2fa_enabled=False,
                updated_buckets=ACCOUNT_BUCKETS,
            ),
        ),
        patch(
            "custom_components.nest_protect._async_subscribe_for_data",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.nest_protect._async_observe_loop",
            new_callable=AsyncMock,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return entry


def _observe(hass: HomeAssistant, entry: MockConfigEntry, state: ThermostatState):
    """Hand a thermostat state over the way the observe loop does."""
    _dispatch_states(
        hass,
        entry.runtime_data.thermostat_state_cache,
        {state.resource_id: state},
        thermostat_signal,
        thermostat_discovery_signal(entry.entry_id),
    )


def _state(hass: HomeAssistant, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"thermostat_DEVICE_T_{key}"
    )
    return hass.states.get(entity_id).state


async def test_discovered_thermostat_gets_its_sensors(hass: HomeAssistant):
    entry = await _setup(hass)
    _observe(hass, entry, THERMOSTAT)
    await hass.async_block_till_done()

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, SERIAL), entry.entry_id
    )
    assert device.name == "Nest Thermostat (Living Room)"
    assert device.model == "Learning Thermostat (3rd gen)"
    assert device.sw_version == "6.2"

    assert float(_state(hass, "backplate_temperature")) == pytest.approx(21.46)
    assert _state(hass, "current_temperature") == "19.0"
    assert _state(hass, "current_humidity") == "48.0"
    assert _state(hass, "active_temperature_sensor") == "Thermostat"
    assert hass.states.get(
        "sensor.living_room_nest_thermostat_living_room_backplate_temperature"
    )
    assert hass.states.get("sensor.living_room_nest_thermostat_living_room_temperature")
    assert hass.states.get("sensor.living_room_nest_thermostat_living_room_humidity")


async def test_thermostat_updates_its_state_and_device(hass: HomeAssistant):
    entry = await _setup(hass)
    _observe(hass, entry, THERMOSTAT)
    await hass.async_block_till_done()

    _observe(
        hass,
        entry,
        replace(
            THERMOSTAT,
            location="Hallway",
            software_version="6.3",
            current_temperature=None,
        ),
    )
    await hass.async_block_till_done()

    assert _state(hass, "current_temperature") == STATE_UNKNOWN
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, SERIAL), entry.entry_id
    )
    assert device.name == "Nest Thermostat (Hallway)"
    assert device.sw_version == "6.3"


@pytest.mark.parametrize(
    ("source", "active", "expected"),
    [
        # A sensor's own label, else its room, else its id.
        (TemperatureSource.SINGLE_SENSOR, ["DEVICE_S1"], "Nursery"),
        (TemperatureSource.SINGLE_SENSOR, ["DEVICE_S2"], "Bedroom"),
        (TemperatureSource.SINGLE_SENSOR, ["DEVICE_S9"], "S9"),
        (
            TemperatureSource.MULTI_SENSOR,
            ["DEVICE_S2", "DEVICE_S1"],
            "Bedroom, Nursery",
        ),
        (None, [], STATE_UNKNOWN),
    ],
)
async def test_active_temperature_sensor_names_the_sensors(
    hass: HomeAssistant, source, active, expected
):
    entry = await _setup(hass)
    _observe(
        hass,
        entry,
        replace(THERMOSTAT, temperature_source=source, active_sensors=active),
    )
    await hass.async_block_till_done()

    assert _state(hass, "active_temperature_sensor") == expected


async def test_discovered_thermostat_is_reported(hass: HomeAssistant):
    entry = await _setup(hass)
    _observe(hass, entry, THERMOSTAT)
    await hass.async_block_till_done()
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, SERIAL), entry.entry_id
    )

    assert not await async_remove_config_entry_device(hass, entry, device)

    diagnostics = await async_get_device_diagnostics(hass, entry, device)
    assert diagnostics["thermostat"]["model"] == "Learning Thermostat (3rd gen)"
    assert diagnostics["thermostat"]["serial_number"] != SERIAL
