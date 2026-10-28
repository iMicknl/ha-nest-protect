"""Tests for picking up new devices and cleaning up removed ones."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect import (
    DOMAIN,
    _async_subscribe_for_data,
    async_remove_config_entry_device,
)
from custom_components.nest_protect.pynest.lock_models import LockBoltState, LockState
from custom_components.nest_protect.pynest.models import (
    Bucket,
    FirstDataAPIResponse,
)

from .conftest import COOKIES, ISSUE_TOKEN
from .test_init import _make_subscribe_data, _make_subscriber_entry_data

LISTED_KEYS = ["topaz.A", "kryptonite.B", "structure.S", "where.S"]


def _make_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)
    return entry


def _buckets_object(keys: list[str], revision: int = 2) -> dict:
    return {
        "object_key": "buckets.user1",
        "object_revision": revision,
        "object_timestamp": revision,
        "value": {"buckets": keys},
    }


async def _run_subscriber(hass, entry, objects, listed_device_keys):
    client, sm = _make_subscriber_entry_data(hass, entry)
    hass.data[DOMAIN][entry.entry_id].listed_device_keys = listed_device_keys
    client.subscribe_for_data = AsyncMock(return_value={"objects": objects})

    with (
        patch("custom_components.nest_protect._register_subscribe_task") as resubscribe,
        patch.object(sm, "ensure_session", new_callable=AsyncMock),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await _async_subscribe_for_data(hass, entry, _make_subscribe_data())

    return resubscribe, reload


async def test_subscriber_reloads_when_device_is_added(hass):
    """A new Protect in the account's bucket list reloads the entry once."""
    entry = _make_entry(hass)
    resubscribe, reload = await _run_subscriber(
        hass,
        entry,
        [_buckets_object([*LISTED_KEYS, "topaz.NEW"])],
        {"topaz.A", "kryptonite.B"},
    )

    reload.assert_called_once_with(entry.entry_id)
    resubscribe.assert_not_called()


async def test_subscriber_reloads_when_device_is_removed(hass):
    """A device dropped from the bucket list reloads so setup can clean it up."""
    entry = _make_entry(hass)
    _, reload = await _run_subscriber(
        hass,
        entry,
        [_buckets_object(["topaz.A", "structure.S"])],
        {"topaz.A", "kryptonite.B"},
    )

    reload.assert_called_once_with(entry.entry_id)


async def test_subscriber_ignores_non_device_bucket_changes(hass):
    """Bucket list changes that don't touch devices must not reload."""
    entry = _make_entry(hass)
    resubscribe, reload = await _run_subscriber(
        hass,
        entry,
        [_buckets_object([*LISTED_KEYS, "message.M"])],
        {"topaz.A", "kryptonite.B"},
    )

    reload.assert_not_called()
    resubscribe.assert_called_once()


MALFORMED_BUCKET_LIST_VALUES = [
    pytest.param({}, id="no-key-list"),
    pytest.param({"buckets": "topaz.A"}, id="key-list-not-a-list"),
    pytest.param(None, id="value-null"),
    pytest.param(["topaz.A"], id="value-not-a-mapping"),
    pytest.param({"buckets": ["topaz.A", None, 1]}, id="non-string-key"),
]


@pytest.mark.parametrize("value", MALFORMED_BUCKET_LIST_VALUES)
async def test_subscriber_ignores_malformed_bucket_list(hass, value):
    """A malformed bucket list must not reload or break the subscriber."""
    entry = _make_entry(hass)
    obj = _buckets_object([])
    obj["value"] = value
    resubscribe, reload = await _run_subscriber(
        hass, entry, [obj], {"topaz.A", "kryptonite.B"}
    )

    reload.assert_not_called()
    resubscribe.assert_called_once()


def _setup_data(
    include_bucket_list: bool = True, bucket_list_value: object = None
) -> FirstDataAPIResponse:
    buckets = [
        {
            "object_key": "topaz.A",
            "object_revision": 1,
            "object_timestamp": 1,
            "value": {"serial_number": "SERIAL_A", "smoke_status": 0},
        },
        {
            # Sparse response: no serial number to match the registry on.
            "object_key": "topaz.SPARSE",
            "object_revision": 1,
            "object_timestamp": 1,
            "value": {"smoke_status": 0},
        },
    ]
    if include_bucket_list:
        bucket_list = _buckets_object(["topaz.A", "topaz.SPARSE"], revision=1)
        if bucket_list_value is not None:
            bucket_list["value"] = bucket_list_value
        buckets.append(bucket_list)
    return FirstDataAPIResponse(
        weather_for_structures={},
        service_urls={"urls": {"transport_url": "https://t.example.com"}},
        _2fa_enabled=False,
        updated_buckets=buckets,
    )


def _register_device(hass, entry, identifier: str, entity_unique_id: str | None):
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, identifier)}
    )
    if entity_unique_id:
        er.async_get(hass).async_get_or_create(
            "sensor",
            DOMAIN,
            entity_unique_id,
            config_entry=entry,
            device_id=device.id,
        )
    return device


async def _setup(hass, entry, data: FirstDataAPIResponse) -> None:
    with (
        patch(
            "custom_components.nest_protect.NestSessionManager.async_setup",
            return_value=data,
        ),
        patch(
            "custom_components.nest_protect._async_subscribe_for_data",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.nest_protect._async_observe_locks_loop",
            new_callable=AsyncMock,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_setup_removes_devices_no_longer_on_account(hass):
    """Setup removes Protects that the account no longer reports."""
    entry = _make_entry(hass)
    current = _register_device(hass, entry, "SERIAL_A", "topaz.A-smoke_status")
    sparse = _register_device(hass, entry, "SERIAL_SPARSE", "topaz.SPARSE-smoke_status")
    stale = _register_device(hass, entry, "SERIAL_OLD", "topaz.OLD-smoke_status")
    stale_sensor = _register_device(
        hass, entry, "kryptonite.OLD", "kryptonite.OLD-current_temperature"
    )
    lock = _register_device(hass, entry, "LOCK_SERIAL", "lock_R1")
    no_entities = _register_device(hass, entry, "UNKNOWN", None)

    await _setup(hass, entry, _setup_data())
    assert entry.state is ConfigEntryState.LOADED

    device_registry = dr.async_get(hass)
    assert device_registry.async_get(current.id)
    assert device_registry.async_get(sparse.id)
    # Locks are only discovered later over gRPC.
    assert device_registry.async_get(lock.id)
    assert device_registry.async_get(no_entities.id)
    assert device_registry.async_get(stale.id) is None
    assert device_registry.async_get(stale_sensor.id) is None
    assert (
        er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, "topaz.OLD-smoke_status"
        )
        is None
    )

    assert hass.data[DOMAIN][entry.entry_id].listed_device_keys == {
        "topaz.A",
        "topaz.SPARSE",
    }

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_without_bucket_list(hass):
    """Without a bucket list in the response, change detection stays off."""
    entry = _make_entry(hass)

    await _setup(hass, entry, _setup_data(include_bucket_list=False))

    assert hass.data[DOMAIN][entry.entry_id].listed_device_keys is None
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(["topaz.A"], id="value-not-a-mapping"),
        pytest.param({"buckets": ["topaz.A", None, 1]}, id="non-string-key"),
    ],
)
async def test_setup_with_malformed_bucket_list(hass, value):
    """A malformed bucket list leaves change detection off without failing setup."""
    entry = _make_entry(hass)

    await _setup(hass, entry, _setup_data(bucket_list_value=value))

    assert entry.state is ConfigEntryState.LOADED
    assert hass.data[DOMAIN][entry.entry_id].listed_device_keys is None
    assert await hass.config_entries.async_unload(entry.entry_id)


def _loaded_entry_with_devices(hass) -> MockConfigEntry:
    entry = _make_entry(hass)
    _make_subscriber_entry_data(hass, entry)
    entry_data = hass.data[DOMAIN][entry.entry_id]
    entry_data.devices = {
        "topaz.A": Bucket("topaz.A", 1, 1, {"serial_number": "SERIAL_A"}),
        "kryptonite.B": Bucket("kryptonite.B", 1, 1, {}),
        "topaz.SPARSE": Bucket("topaz.SPARSE", 1, 1, {}),
    }
    entry_data.lock_state_cache = {
        "R1": LockState(
            resource_id="R1",
            name="Front Door",
            serial_number="LOCK_SERIAL",
            bolt_state=LockBoltState.LOCKED,
        )
    }
    return entry


async def test_remove_device_blocked_while_reported(hass):
    """Devices the account still reports cannot be removed."""
    entry = _loaded_entry_with_devices(hass)
    device_registry = dr.async_get(hass)

    for identifier, unique_id in (
        ("SERIAL_A", None),
        ("kryptonite.B", None),
        ("LOCK_SERIAL", None),
        ("SERIAL_SPARSE", "topaz.SPARSE-smoke_status"),
    ):
        device = _register_device(hass, entry, identifier, unique_id)
        assert not await async_remove_config_entry_device(
            hass, entry, device_registry.async_get(device.id)
        ), identifier


async def test_remove_home_blocked_while_reported(hass):
    """A home with a safety summary the account still reports cannot be removed."""
    entry = _loaded_entry_with_devices(hass)
    hass.data[DOMAIN][entry.entry_id].safety_summaries = {
        "safety_summary.HOME": Bucket("safety_summary.HOME", 1, 1, {})
    }
    device = _register_device(
        hass, entry, "safety_summary.HOME", "safety_summary.HOME-total_warnings"
    )

    assert not await async_remove_config_entry_device(hass, entry, device)


async def test_remove_device_allowed_when_gone(hass):
    """Devices the account no longer reports can be removed."""
    entry = _loaded_entry_with_devices(hass)
    device = _register_device(hass, entry, "SERIAL_OLD", "topaz.OLD-smoke_status")

    assert await async_remove_config_entry_device(hass, entry, device)


async def test_remove_device_allowed_when_entry_not_loaded(hass):
    """Without loaded data there is nothing to protect the device from."""
    entry = _make_entry(hass)
    device = MagicMock(id="dev", identifiers={(DOMAIN, "SERIAL_A")})

    assert await async_remove_config_entry_device(hass, entry, device)
