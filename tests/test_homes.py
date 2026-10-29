"""Tests for choosing which Nest homes a config entry covers."""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect import (
    _async_observe_locks_loop,
    _async_subscribe_for_data,
)
from custom_components.nest_protect.const import CONF_HOMES, DOMAIN
from custom_components.nest_protect.homes import (
    excluded_bucket_keys,
    homes_options,
    is_excluded_resource,
)
from custom_components.nest_protect.pynest.lock_models import LockBoltState, LockState
from custom_components.nest_protect.pynest.models import FirstDataAPIResponse

from .conftest import COOKIES, ISSUE_TOKEN
from .test_config_flow import VALID_COOKIES, VALID_ISSUE_TOKEN


def _bucket(object_key: str, value: dict) -> dict:
    return {
        "object_key": object_key,
        "object_revision": 1,
        "object_timestamp": 1,
        "value": value,
    }


ACCOUNT_BUCKETS = [
    _bucket("structure.home-1", {"name": "Beach House", "swarm": ["topaz.P1"]}),
    _bucket(
        "structure.home-2",
        # The sensor has no structure_id and the lock has no bucket: only the
        # swarm places them in this home.
        {"name": "Office", "swarm": ["topaz.P2", "kryptonite.K2", "yale.LOCK2"]},
    ),
    _bucket("topaz.P1", {"structure_id": "home-1", "serial_number": "SERIAL_P1"}),
    _bucket("topaz.P2", {"structure_id": "home-2", "serial_number": "SERIAL_P2"}),
    _bucket("kryptonite.K2", {"serial_number": "SERIAL_K2", "battery_level": 90}),
    _bucket("widget_track.P1", {"online": True}),
    _bucket("widget_track.P2", {"online": True}),
    _bucket("where.home-1", {"wheres": [{"where_id": "w1", "name": "Hall"}]}),
    _bucket("where.home-2", {"wheres": [{"where_id": "w2", "name": "Lobby"}]}),
    _bucket("safety_summary.home-1", {"total_critical_failures": 0}),
    _bucket("safety_summary.home-2", {"total_critical_failures": 0}),
    _bucket("user.1", {"email": "user@example.com"}),
]


def _first_data(buckets: list[dict] = ACCOUNT_BUCKETS) -> FirstDataAPIResponse:
    return FirstDataAPIResponse(
        weather_for_structures={},
        service_urls={"urls": {"transport_url": "https://t.example.com"}},
        _2fa_enabled=False,
        updated_buckets=buckets,
    )


def _entry(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        unique_id="user.1",
        data={
            "issue_token": ISSUE_TOKEN,
            "cookies": COOKIES,
            "account_type": "production",
        },
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


@contextmanager
def _patched_setup() -> Iterator[AsyncMock]:
    """Set up entries from ACCOUNT_BUCKETS; yields the subscriber mock."""
    with (
        patch(
            "custom_components.nest_protect.NestSessionManager.async_setup",
            side_effect=_first_data,
        ),
        patch(
            "custom_components.nest_protect._async_subscribe_for_data",
            new_callable=AsyncMock,
        ) as subscribe,
        patch(
            "custom_components.nest_protect._async_observe_locks_loop",
            new_callable=AsyncMock,
        ),
    ):
        yield subscribe


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> AsyncMock:
    with _patched_setup() as subscribe:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return subscribe


def _device(
    hass: HomeAssistant, entry: MockConfigEntry, identifier: str
) -> dr.DeviceEntry | None:
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, identifier), entry.entry_id
    )


def _lock(resource_id: str) -> LockState:
    return LockState(
        resource_id=resource_id,
        name="Front Door",
        serial_number=f"SERIAL_{resource_id}",
        bolt_state=LockBoltState.LOCKED,
    )


def test_unselected_home_buckets_are_excluded():
    """Device, home and swarm keys of other homes are excluded, unknown ones kept."""
    buckets = _first_data(
        [*ACCOUNT_BUCKETS, _bucket("topaz.UNKNOWN", {"serial_number": "X"})]
    ).updated_buckets

    assert excluded_bucket_keys(buckets, ["home-1"]) == {
        "structure.home-2",
        "topaz.P2",
        "kryptonite.K2",
        "yale.LOCK2",
        "widget_track.P2",
        "where.home-2",
        "safety_summary.home-2",
    }


def test_lock_is_matched_by_device_id():
    """Locks are placed through the swarm entry with the same device ID."""
    excluded = {"structure.home-2", "yale.LOCK2"}

    assert is_excluded_resource("DEVICE_LOCK2", excluded)
    assert is_excluded_resource("DEVICE_lock2", excluded)
    assert not is_excluded_resource("DEVICE_LOCK1", excluded)


def test_selecting_every_home_stores_no_filter():
    """Selecting every home keeps tracking homes added to the account later."""
    assert homes_options(["home-2", "home-1"], ["home-1", "home-2"]) == {}
    assert homes_options(["home-2"], ["home-1", "home-2"]) == {CONF_HOMES: ["home-2"]}


async def test_without_selection_every_home_is_set_up(hass: HomeAssistant):
    """Existing entries have no option and keep every home."""
    entry = _entry(hass)
    await _setup(hass, entry)

    assert set(entry.runtime_data.devices) == {"topaz.P1", "topaz.P2", "kryptonite.K2"}
    assert entry.runtime_data.homes == {"home-1": "Beach House", "home-2": "Office"}
    assert entry.runtime_data.excluded_keys == set()


async def test_only_selected_home_is_set_up(hass: HomeAssistant):
    """Devices, home sensors and subscriptions of other homes are left out."""
    entry = _entry(hass, {CONF_HOMES: ["home-1"]})
    subscribe = await _setup(hass, entry)

    data = entry.runtime_data
    assert set(data.devices) == {"topaz.P1"}
    assert set(data.safety_summaries) == {"safety_summary.home-1"}
    assert data.areas == {"w1": "Hall"}
    # Every home is still offered in the options.
    assert data.homes == {"home-1": "Beach House", "home-2": "Office"}

    assert _device(hass, entry, "SERIAL_P1") is not None
    assert _device(hass, entry, "SERIAL_P2") is None
    assert _device(hass, entry, "SERIAL_K2") is None
    assert _device(hass, entry, "structure.home-2") is None

    subscribed = {b.object_key for b in subscribe.call_args.args[2].updated_buckets}
    assert subscribed == {
        "structure.home-1",
        "topaz.P1",
        "widget_track.P1",
        "where.home-1",
        "safety_summary.home-1",
        "user.1",
    }


async def test_subscriber_ignores_unselected_home(hass: HomeAssistant):
    """Updates for another home's devices don't create or update anything."""
    entry = _entry(hass, {CONF_HOMES: ["home-1"]})
    subscribe = await _setup(hass, entry)
    data = entry.runtime_data
    first_data = subscribe.call_args.args[2]

    client = data.client
    client.nest_session = MagicMock(access_token="token", userid="1")
    client.subscribe_for_data = AsyncMock(
        return_value={
            "objects": [
                {**_bucket("topaz.P2", {"smoke_status": 2}), "type": "topaz"},
                {**_bucket("topaz.P1", {"smoke_status": 1}), "type": "topaz"},
            ]
        }
    )
    with (
        patch.object(data.session_manager, "ensure_session", AsyncMock()),
        patch("custom_components.nest_protect._register_subscribe_task"),
    ):
        await _async_subscribe_for_data(hass, entry, first_data)

    assert set(data.devices) == {"topaz.P1"}
    assert data.devices["topaz.P1"].value == {"smoke_status": 1}


async def test_lock_of_unselected_home_is_skipped_and_removed(hass: HomeAssistant):
    """Locks listed in another home's swarm are not added, and their device goes."""
    entry = _entry(hass, {CONF_HOMES: ["home-1"]})
    await _setup(hass, entry)
    device_registry = dr.async_get(hass)
    stale = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "SERIAL_DEVICE_LOCK2")},
    )

    async def observe_locks():
        yield {
            "DEVICE_LOCK1": _lock("DEVICE_LOCK1"),
            "DEVICE_LOCK2": _lock("DEVICE_LOCK2"),
        }

    entry.runtime_data.grpc_lock_client.observe_locks = observe_locks
    await _async_observe_locks_loop(hass, entry)
    await hass.async_block_till_done()

    assert set(entry.runtime_data.lock_state_cache) == {"DEVICE_LOCK1"}
    assert device_registry.async_get(stale.id) is None
    assert _device(hass, entry, "SERIAL_DEVICE_LOCK1") is not None


async def test_options_flow_deselects_home_and_removes_its_devices(
    hass: HomeAssistant,
):
    """Changing the homes reloads the entry and removes the other home's devices."""
    entry = _entry(hass)
    with _patched_setup():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert _device(hass, entry, "SERIAL_P2") is not None
        assert _device(hass, entry, "structure.home-2") is not None

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_HOMES: ["home-1"]}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_HOMES: ["home-1"]}
    assert entry.state is ConfigEntryState.LOADED
    assert set(entry.runtime_data.devices) == {"topaz.P1"}
    assert _device(hass, entry, "SERIAL_P1") is not None
    assert _device(hass, entry, "SERIAL_P2") is None
    assert _device(hass, entry, "SERIAL_K2") is None
    assert _device(hass, entry, "structure.home-2") is None
    assert _device(hass, entry, "structure.home-1") is not None
    entity_registry = er.async_get(hass)
    assert not entity_registry.async_get_entity_id(
        "sensor", DOMAIN, "safety_summary.home-2-total_critical_failures"
    )


async def test_options_flow_requires_a_home(hass: HomeAssistant):
    """An empty selection is rejected."""
    entry = _entry(hass)
    await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_HOMES: []}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HOMES: "no_homes_selected"}


async def test_options_flow_aborts_when_not_loaded(hass: HomeAssistant):
    """The homes come from the loaded entry."""
    entry = _entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"


def _flow_client(buckets: list[dict]) -> MagicMock:
    client = MagicMock()
    client.refreshed_cookies = None
    client.get_access_token_from_cookies = AsyncMock(
        return_value=MagicMock(access_token="google-token")
    )
    client.authenticate = AsyncMock(
        return_value=MagicMock(access_token="nest-token", userid="1", user="user.1")
    )
    client.get_first_data = AsyncMock(return_value=_first_data(buckets))
    return client


async def _sign_in(hass: HomeAssistant, buckets: list[dict]) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"account_type": "production"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"method": "manual"}
    )
    with patch(
        "custom_components.nest_protect.config_flow.NestClient",
        return_value=_flow_client(buckets),
    ):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"issue_token": VALID_ISSUE_TOKEN, "cookies": VALID_COOKIES},
        )


async def test_config_flow_asks_for_homes(hass: HomeAssistant):
    """An account with several homes chooses them before the entry is created."""
    result = await _sign_in(hass, ACCOUNT_BUCKETS)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "homes"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_HOMES: []}
    )
    assert result["errors"] == {CONF_HOMES: "no_homes_selected"}

    with patch("custom_components.nest_protect.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_HOMES: ["home-2"]}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Nest Protect (user@example.com)"
    assert result["data"]["cookies"] == VALID_COOKIES
    assert result["options"] == {CONF_HOMES: ["home-2"]}


async def test_config_flow_all_homes_stores_no_filter(hass: HomeAssistant):
    """Keeping every home selected creates the entry without a filter."""
    result = await _sign_in(hass, ACCOUNT_BUCKETS)

    with patch("custom_components.nest_protect.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_HOMES: ["home-1", "home-2"]}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {}


async def test_config_flow_single_home_skips_step(hass: HomeAssistant):
    """An account with one home is created straight away."""
    buckets = [b for b in ACCOUNT_BUCKETS if b["object_key"] != "structure.home-2"]

    with patch("custom_components.nest_protect.async_setup_entry", return_value=True):
        result = await _sign_in(hass, buckets)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {}


async def test_reauth_keeps_home_selection(hass: HomeAssistant):
    """Reauthenticating doesn't ask for homes again or reset the selection."""
    entry = _entry(hass, {CONF_HOMES: ["home-1"]})
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"method": "manual"}
    )

    with (
        patch(
            "custom_components.nest_protect.config_flow.NestClient",
            return_value=_flow_client(ACCOUNT_BUCKETS),
        ),
        patch("custom_components.nest_protect.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"issue_token": VALID_ISSUE_TOKEN, "cookies": VALID_COOKIES},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.options == {CONF_HOMES: ["home-1"]}
