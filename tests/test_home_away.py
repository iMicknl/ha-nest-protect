"""Tests for the Home/Away switch."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect import (
    HomeAssistantNestProtectData,
    _async_subscribe_for_data,
    async_remove_config_entry_device,
)
from custom_components.nest_protect.const import DOMAIN
from custom_components.nest_protect.pynest.exceptions import NestLockCommandException
from custom_components.nest_protect.pynest.models import Bucket, FirstDataAPIResponse

from .conftest import COOKIES, ISSUE_TOKEN


def _bucket(object_key: str, value: dict) -> dict:
    return {
        "object_key": object_key,
        "object_revision": 1,
        "object_timestamp": 1,
        "value": value,
    }


ACCOUNT_BUCKETS = [
    _bucket("structure.home-1", {"name": "Beach House", "away": False}),
    _bucket("structure.home-2", {"name": "Office", "away": True}),
    _bucket("structure.home-3", {"name": "Cabin"}),
]


async def _setup(hass: HomeAssistant, buckets: list[dict]) -> MockConfigEntry:
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
                updated_buckets=buckets,
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


def _entity_id(hass: HomeAssistant, structure_key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        SWITCH_DOMAIN, DOMAIN, f"{structure_key}-away"
    )


@pytest.fixture
def grpc_commands(hass: HomeAssistant):
    """Stub the gateway, as if the observe stream had mapped home-1."""
    resource_ids = {"structure.home-1": "STRUCTURE_ABC"}
    with (
        patch(
            "custom_components.nest_protect.pynest.grpc_client.GrpcClient.structure_resource_id",
            side_effect=resource_ids.get,
        ),
        patch(
            "custom_components.nest_protect.pynest.grpc_client.GrpcClient.send_structure_mode_command",
            new_callable=AsyncMock,
        ) as send,
        patch(
            "custom_components.nest_protect.NestSessionManager.ensure_session",
            new_callable=AsyncMock,
        ),
    ):
        yield send


async def test_switch_per_home_reports_away(hass: HomeAssistant):
    """Every home gets an Away mode switch on its home device."""
    entry = await _setup(hass, ACCOUNT_BUCKETS)

    assert hass.states.get("switch.beach_house_away_mode").state == STATE_OFF
    assert hass.states.get("switch.office_away_mode").state == STATE_ON
    assert hass.states.get("switch.cabin_away_mode").state == STATE_UNKNOWN

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "structure.home-1"), entry.entry_id
    )
    entity = er.async_get(hass).async_get(_entity_id(hass, "structure.home-1"))
    assert entity.device_id == device.id
    assert device.name == "Beach House"


@pytest.mark.parametrize(
    ("service", "away"), [(SERVICE_TURN_ON, True), (SERVICE_TURN_OFF, False)]
)
async def test_turning_the_switch_sends_the_structure_mode(
    hass: HomeAssistant, grpc_commands: AsyncMock, service: str, away: bool
):
    await _setup(hass, ACCOUNT_BUCKETS)

    await hass.services.async_call(
        SWITCH_DOMAIN,
        service,
        {ATTR_ENTITY_ID: "switch.beach_house_away_mode"},
        blocking=True,
    )

    grpc_commands.assert_awaited_once_with("STRUCTURE_ABC", away)


async def test_switch_refuses_until_the_home_is_mapped(
    hass: HomeAssistant, grpc_commands: AsyncMock
):
    """Without the gateway's id for the home there is nothing to send to."""
    await _setup(hass, ACCOUNT_BUCKETS)

    with pytest.raises(HomeAssistantError, match="hasn't reported this home"):
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: "switch.office_away_mode"},
            blocking=True,
        )

    grpc_commands.assert_not_called()


async def test_rejected_command_surfaces_as_an_error(
    hass: HomeAssistant, grpc_commands: AsyncMock
):
    await _setup(hass, ACCOUNT_BUCKETS)
    grpc_commands.side_effect = NestLockCommandException("code=7")

    with pytest.raises(HomeAssistantError, match="code=7"):
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: "switch.beach_house_away_mode"},
            blocking=True,
        )


async def test_home_device_without_protect_cannot_be_removed(hass: HomeAssistant):
    """The Home/Away switch keeps a home without a Protect reported."""
    entry = await _setup(hass, ACCOUNT_BUCKETS)
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "structure.home-2"), entry.entry_id
    )

    assert not await async_remove_config_entry_device(hass, entry, device)


async def test_subscription_updates_the_structure(hass: HomeAssistant):
    """Pushed structure buckets replace the stored bucket and notify entities."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    client = MagicMock()
    sm = MagicMock(ensure_session=AsyncMock(), refreshed_cookies=None)
    initial = Bucket(**_bucket("structure.home-1", {"away": False}))
    entry_data = HomeAssistantNestProtectData(
        devices={},
        areas={},
        client=client,
        session_manager=sm,
        grpc_client=MagicMock(),
        structures={initial.object_key: initial},
        stream_health=MagicMock(),
    )
    entry.runtime_data = entry_data
    client.subscribe_for_data = AsyncMock(
        return_value={"objects": [_bucket("structure.home-1", {"away": True})]}
    )
    data = MagicMock()
    data.updated_buckets = []

    with (
        patch("custom_components.nest_protect._register_subscribe_task"),
        patch("custom_components.nest_protect.async_dispatcher_send") as dispatch,
    ):
        await _async_subscribe_for_data(hass, entry, data)

    updated = entry_data.structures["structure.home-1"]
    assert updated.value == {"away": True}
    dispatch.assert_called_once_with(hass, "structure.home-1", updated)
