"""Nest Protect integration."""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import httpx
from aiohttp import (
    ClientConnectorError,
    ClientError,
    ClientOSError,
    ConnectionTimeoutError,
    ServerDisconnectedError,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import (
    BACKOFF_INTERVALS,
    CONF_ACCOUNT_TYPE,
    CONF_COOKIES,
    CONF_ISSUE_TOKEN,
    CONF_REFRESH_TOKEN,
    DEVICE_CACHE_SAVE_DELAY,
    DOMAIN,
    LOCK_AUTH_SETTLE_SECONDS,
    LOGGER,
    MAX_AUTH_FAILURES,
    PLATFORMS,
    PROTOBUF_RECONNECT_INITIAL_DELAY,
    PROTOBUF_RECONNECT_MAX_DELAY,
    PROTOBUF_STREAM_HEALTHY_SECONDS,
    PYNEST_ERROR_MIN_SECONDS,
    RECONNECT_INTERVALS,
    SERVICE_ERROR_MIN_SECONDS,
    STORAGE_KEY_DEVICES_FORMAT,
    STORAGE_KEY_FORMAT,
    STORAGE_VERSION,
    SUBSCRIBE_FAST_RESPONSE_WARNING,
    SUBSCRIBE_HEALTHY_SECONDS,
    SUBSCRIBE_MIN_INTERVAL_SECONDS,
)
from .lock import discovery_signal, lock_signal
from .pynest.client import NestClient
from .pynest.const import NEST_ENVIRONMENTS
from .pynest.enums import BucketType, Environment
from .pynest.exceptions import (
    BadCredentialsException,
    EmptyResponseException,
    NestLockAuthException,
    NestServiceException,
    NotAuthenticatedException,
    PynestException,
)
from .pynest.grpc_client import GrpcLockClient
from .pynest.lock_models import LockState
from .pynest.models import (
    Bucket,
    FirstDataAPIResponse,
    TopazBucket,
    WhereBucketValue,
)
from .pynest.protobuf import (
    NEST_KRYPTONITE_RESOURCE,
    ProtobufDeviceUpdate,
    ProtobufStructureUpdate,
)
from .session import NestSessionManager
from .stream_health import StreamHealth
from .thermostat import (
    THERMOSTAT_BUCKET_PREFIX,
    THERMOSTAT_CACHE_KEYS,
    is_discoverable_thermostat,
    thermostat_cache_entry,
    thermostat_discovery_signal,
)

DEVICE_BUCKET_TYPES = {BucketType.TOPAZ, BucketType.KRYPTONITE}


@dataclass
class HomeAssistantNestProtectData:
    """Nest Protect data stored in the Home Assistant data object."""

    devices: dict[str, Bucket]
    areas: dict[str, str]
    client: NestClient
    session_manager: NestSessionManager
    grpc_lock_client: GrpcLockClient
    stream_health: StreamHealth
    subscription_task: asyncio.Task | None = None
    lock_observe_task: asyncio.Task | None = None
    lock_state_cache: dict[str, LockState] = field(default_factory=dict)
    protobuf_observe_task: asyncio.Task | None = None
    protobuf_structure_map: dict[str, str] | None = None
    # Thermostat bucket keys already announced to the sensor platform.
    protobuf_thermostats: set[str] = field(default_factory=set)
    # Persists protobuf-only devices, so a reload doesn't have to wait for the
    # observe stream before it can recreate their entities.
    device_store: Store | None = None
    connection_failures: int = 0
    # Consecutive subscribes Nest answered with data almost immediately.
    fast_data_responses: int = 0
    widget_tracks: dict[str, Bucket] = field(default_factory=dict)
    safety_summaries: dict[str, Bucket] = field(default_factory=dict)
    structures: dict[str, Bucket] = field(default_factory=dict)
    structure_names: dict[str, str] = field(default_factory=dict)
    # Device keys from the account's bucket list at setup; None if not returned.
    listed_device_keys: set[str] | None = None


type NestProtectConfigEntry = ConfigEntry[HomeAssistantNestProtectData]


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    """Migrate old Config entries."""
    LOGGER.debug("Migrating from version %s", config_entry.version)

    if config_entry.version == 1:
        hass.config_entries.async_update_entry(
            config_entry,
            data={**config_entry.data, CONF_ACCOUNT_TYPE: Environment.PRODUCTION},
            version=2,
        )

    if config_entry.version == 2:  # noqa: PLR2004
        # v3 introduced cookie authentication; refresh-token entries keep working.
        hass.config_entries.async_update_entry(config_entry, version=3)

    LOGGER.debug("Migration to version %s successful", config_entry.version)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: NestProtectConfigEntry):
    """Set up Nest Protect from a config entry."""
    issue_token = entry.data.get(CONF_ISSUE_TOKEN)
    cookies = entry.data.get(CONF_COOKIES)
    refresh_token = entry.data.get(CONF_REFRESH_TOKEN)

    session = async_create_clientsession(hass)
    account_type = entry.data.get(CONF_ACCOUNT_TYPE, Environment.PRODUCTION)
    client = NestClient(session=session, environment=NEST_ENVIRONMENTS[account_type])

    client.issue_token = issue_token
    client.cookies = cookies
    client.refresh_token = refresh_token

    store = Store(
        hass, STORAGE_VERSION, STORAGE_KEY_FORMAT.format(entry_id=entry.entry_id)
    )

    session_manager = NestSessionManager(
        client=client,
        store=store,
        on_cookies_refreshed=partial(_persist_refreshed_cookies, hass, entry),
    )

    try:
        data = await session_manager.async_setup()
    except (TimeoutError, ClientError) as exception:
        raise ConfigEntryNotReady from exception
    except NestServiceException as exception:
        raise ConfigEntryNotReady(
            f"Nest service temporarily unavailable: {exception}"
        ) from exception
    except BadCredentialsException as exception:
        raise ConfigEntryAuthFailed from exception
    except Exception as exception:  # pylint: disable=broad-except
        LOGGER.exception("Unknown exception.")
        raise ConfigEntryNotReady from exception

    if data is None:
        raise ConfigEntryAuthFailed("No credentials available")

    device_buckets: list[Bucket] = []
    structure_buckets: list[Bucket] = []
    widget_tracks: dict[str, Bucket] = {}
    safety_summary_buckets: list[Bucket] = []
    areas: dict[str, str] = {}
    structure_names: dict[str, str] = {}
    listed_device_keys: set[str] | None = None

    for bucket in data.updated_buckets:
        if bucket.type in DEVICE_BUCKET_TYPES:
            device_buckets.append(bucket)

        if bucket.type == BucketType.STRUCTURE:
            structure_buckets.append(bucket)

        if bucket.type == BucketType.WIDGET_TRACK:
            widget_tracks[bucket.object_key] = bucket
        if bucket.type == BucketType.SAFETY_SUMMARY:
            safety_summary_buckets.append(bucket)

        if bucket.type == BucketType.STRUCTURE and (name := bucket.value.get("name")):
            structure_names[bucket.object_key.removeprefix("structure.")] = name
        if bucket.type == BucketType.BUCKETS:
            listed_device_keys = _listed_device_keys(bucket.value)

        if bucket.type == BucketType.WHERE and isinstance(
            bucket.value, WhereBucketValue
        ):
            bucket_value = bucket.value
            for area in bucket_value.wheres:
                areas[area.where_id] = area.name

    devices: dict[str, Bucket] = {b.object_key: b for b in device_buckets}
    structures: dict[str, Bucket] = {b.object_key: b for b in structure_buckets}

    # Nest returns a safety summary for every home, including homes without a
    # Protect, where it would only ever report zeros.
    protect_structure_ids = {
        b.value.get("structure_id")
        for b in device_buckets
        if b.type == BucketType.TOPAZ
    }
    safety_summaries: dict[str, Bucket] = {
        b.object_key: b
        for b in safety_summary_buckets
        if b.object_key.removeprefix("safety_summary.") in protect_structure_ids
    }

    entry_data = HomeAssistantNestProtectData(
        devices=devices,
        structures=structures,
        areas=areas,
        client=client,
        session_manager=session_manager,
        grpc_lock_client=GrpcLockClient(client),
        widget_tracks=widget_tracks,
        safety_summaries=safety_summaries,
        structure_names=structure_names,
        stream_health=StreamHealth(hass, entry.entry_id),
        listed_device_keys=listed_device_keys,
        device_store=Store(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY_DEVICES_FORMAT.format(entry_id=entry.entry_id),
        ),
    )
    entry.runtime_data = entry_data
    # The first data fetch above counts as a response and starts the deadline.
    entry_data.stream_health.async_record_response()
    entry.async_on_unload(entry_data.stream_health.async_stop)

    await _async_restore_protobuf_thermostats(entry_data)
    _seed_protobuf_observe_state(entry_data)

    _async_remove_stale_devices(hass, entry, entry_data)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _register_subscribe_task(hass, entry, data)

    entry_data.protobuf_observe_task = entry.async_create_background_task(
        hass,
        _async_observe_for_protobuf_data(hass, entry),
        name=f"{DOMAIN}_protobuf_observe_{entry.entry_id}",
    )

    entry_data.lock_observe_task = entry.async_create_background_task(
        hass,
        _async_observe_locks_loop(hass, entry),
        name=f"{DOMAIN}_lock_observe_{entry.entry_id}",
    )

    return True


def _listed_device_keys(value: Any) -> set[str] | None:
    """Return the Protect and temperature sensor keys from a bucket list."""
    keys = value.get("buckets") if isinstance(value, dict) else None
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        return None
    return {key for key in keys if key.partition(".")[0] in DEVICE_BUCKET_TYPES}


def _device_bucket_keys(hass: HomeAssistant, device_id: str) -> set[str]:
    """Return the bucket keys of a device's entities, which prefix their unique IDs."""
    return {
        entity.unique_id.partition("-")[0]
        for entity in er.async_entries_for_device(
            er.async_get(hass), device_id, include_disabled_entities=True
        )
    }


def _is_reported_device(
    hass: HomeAssistant, entry_data: HomeAssistantNestProtectData, device: DeviceEntry
) -> bool:
    """Return whether the account still reports this device."""
    reported = {lock.serial_number for lock in entry_data.lock_state_cache.values()}
    for key, bucket in entry_data.devices.items():
        reported.add(key)
        if serial_number := bucket.value.get("serial_number"):
            reported.add(serial_number)

    if any(
        domain == DOMAIN and identifier in reported
        for domain, identifier in device.identifiers
    ):
        return True

    # Sparse responses can omit the serial number the device was registered with.
    # Homes are devices too, keyed by their safety summary bucket.
    return not _device_bucket_keys(hass, device.id).isdisjoint(
        entry_data.devices.keys() | entry_data.safety_summaries.keys()
    )


@callback
def _async_remove_stale_devices(
    hass: HomeAssistant, entry: ConfigEntry, entry_data: HomeAssistantNestProtectData
) -> None:
    """Remove Protects and temperature sensors that left the account."""
    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        if _is_reported_device(hass, entry_data, device):
            continue

        # Only Protect and temperature sensor devices are known at this point;
        # locks are discovered later over gRPC, so anything else is kept.
        bucket_keys = _device_bucket_keys(hass, device.id)
        if not bucket_keys or any(
            key.partition(".")[0] not in DEVICE_BUCKET_TYPES for key in bucket_keys
        ):
            continue

        LOGGER.debug("Removing device %s that is no longer on the account", device.id)
        device_registry.async_remove_device(device.id)


async def _async_observe_locks_loop(
    hass: HomeAssistant, entry: NestProtectConfigEntry
) -> None:
    """Consume the gRPC observe stream and dispatch lock updates.

    Returns — ending the background task — when the observer reports that this
    account has no locks, so accounts without a Nest x Yale lock stop talking to
    the gateway after the first stream. Auth failures are retried with a session
    refresh, and escalate to re-auth after MAX_AUTH_FAILURES.

    Auth health is tracked on the shared NestSessionManager rather than locally,
    so this loop and the REST subscriber can't each sit below the threshold
    while the session is thoroughly broken.
    """
    entry_data = entry.runtime_data
    cache = entry_data.lock_state_cache
    sm = entry_data.session_manager

    # Every new stream starts by sending the current lock state, so a batch alone
    # doesn't prove the session works: a stream rejected right after it would
    # otherwise reset the streak and refresh the session forever.
    auth_failures = 0
    last_auth_failure = 0.0

    while True:
        try:
            async for batch in entry_data.grpc_lock_client.observe_locks():
                if (
                    not auth_failures
                    or time.monotonic() - last_auth_failure >= LOCK_AUTH_SETTLE_SECONDS
                ):
                    auth_failures = 0
                    # REST parse errors are the subscriber's own concern.
                    sm.record_auth_success()
                new_locks: dict[str, LockState] = {}
                for resource_id, lock_state in batch.items():
                    previous = cache.get(resource_id)
                    cache[resource_id] = lock_state
                    if previous is None:
                        new_locks[resource_id] = lock_state
                    else:
                        async_dispatcher_send(
                            hass, lock_signal(resource_id), lock_state
                        )
                if new_locks:
                    async_dispatcher_send(
                        hass, discovery_signal(entry.entry_id), new_locks
                    )
        except asyncio.CancelledError:
            raise
        except NestLockAuthException as err:
            auth_failures += 1
            last_auth_failure = time.monotonic()
            sm.record_auth_failure()
            if auth_failures >= MAX_AUTH_FAILURES or sm.should_trigger_reauth:
                LOGGER.warning(
                    "Lock observer: %d consecutive auth failures, triggering "
                    "re-authentication",
                    max(auth_failures, sm.consecutive_auth_failures),
                )
                entry.async_start_reauth(hass)
                return

            delay = BACKOFF_INTERVALS[min(auth_failures, len(BACKOFF_INTERVALS)) - 1]
            LOGGER.debug(
                "Lock observer: credentials rejected (%r), refreshing session in %ds",
                err,
                delay,
            )
            await asyncio.sleep(delay)
            if not await _async_recover_session(hass, entry, entry_data):
                return
        except Exception:
            LOGGER.exception("Lock observe loop failed unexpectedly")
            return
        else:
            # observe_locks() only ends the iteration when it has concluded
            # there is nothing to watch on this account.
            LOGGER.debug("Lock observer: no locks on this account, stopping")
            return


async def async_unload_entry(
    hass: HomeAssistant, entry: NestProtectConfigEntry
) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Cancel background tasks only after successful platform unload
        entry_data = entry.runtime_data
        if entry_data.subscription_task:
            entry_data.subscription_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await entry_data.subscription_task
        if entry_data.lock_observe_task:
            entry_data.lock_observe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await entry_data.lock_observe_task
        if entry_data.protobuf_observe_task:
            entry_data.protobuf_observe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await entry_data.protobuf_observe_task

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up persisted session and device data when the entry is removed."""
    for key_format in (STORAGE_KEY_FORMAT, STORAGE_KEY_DEVICES_FORMAT):
        store = Store(hass, STORAGE_VERSION, key_format.format(entry_id=entry.entry_id))
        await store.async_remove()


@callback
def _persist_refreshed_cookies(
    hass: HomeAssistant, entry: NestProtectConfigEntry, new_cookies: str
) -> None:
    """Persist Google-rotated cookies back to the config entry.

    Google may rotate OAuth cookies during ``get_access_token_from_cookies``.
    Without writing them back, a setup retry or HA restart would use stale
    cookies and eventually force re-authentication.
    """
    if new_cookies == entry.data.get(CONF_COOKIES):
        return

    LOGGER.debug("Persisting refreshed Nest cookies")
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_COOKIES: new_cookies},
    )


async def _async_recover_session(
    hass: HomeAssistant,
    entry: NestProtectConfigEntry,
    entry_data: HomeAssistantNestProtectData,
) -> bool:
    """Refresh rejected credentials and report whether background work can resume."""
    if not _is_loaded(entry):
        return False

    # Exceptions raised inside a transport's except block bypass its other handlers.
    try:
        await entry_data.session_manager.async_refresh_session()
    except BadCredentialsException:
        entry.async_start_reauth(hass)
        return False
    except (
        TimeoutError,
        ClientError,
        NestServiceException,
        NotAuthenticatedException,
        PynestException,
    ) as err:
        LOGGER.debug("Session recovery failed; retrying background updates: %s", err)
    except Exception:
        # Escaping here would end the background task with nothing to restart it.
        LOGGER.exception("Unexpected error during session recovery; retrying later")

    return _is_loaded(entry)


def _is_loaded(entry: NestProtectConfigEntry) -> bool:
    """Return whether the entry's runtime data is still in place."""
    # HA deletes runtime_data once the entry has been unloaded.
    return hasattr(entry, "runtime_data")


def _register_subscribe_task(
    hass: HomeAssistant, entry: NestProtectConfigEntry, data: FirstDataAPIResponse
) -> asyncio.Task | None:
    """Create a new subscription task and update the reference."""
    if not _is_loaded(entry):
        return None

    entry_data = entry.runtime_data
    task = entry.async_create_background_task(
        hass,
        _async_subscribe_for_data(hass, entry, data),
        name=f"{DOMAIN}_subscribe_{entry.entry_id}",
    )
    entry_data.subscription_task = task
    return task


def _next_observe_delay(delay: int, stream_duration: float) -> int:
    """Pace the next observe reconnect.

    A stream that stayed up long enough to be doing its job resets the delay;
    anything shorter doubles it, so an account the gateway keeps hanging up on
    settles into a slow retry instead of hammering it for weeks.
    """
    if stream_duration >= PROTOBUF_STREAM_HEALTHY_SECONDS:
        return PROTOBUF_RECONNECT_INITIAL_DELAY
    return min(delay * 2, PROTOBUF_RECONNECT_MAX_DELAY)


async def _async_observe_for_protobuf_data(
    hass: HomeAssistant, entry: NestProtectConfigEntry
) -> None:
    """Observe protobuf data: Home/Away, thermostats and temperature sensors."""
    delay = PROTOBUF_RECONNECT_INITIAL_DELAY

    while _is_loaded(entry):
        entry_data = entry.runtime_data
        sm = entry_data.session_manager
        started = hass.loop.time()
        received = False

        try:
            await sm.ensure_session()

            async for update in entry_data.client.observe_for_structure_updates(
                entry_data.client.nest_session.access_token,
            ):
                if not received:
                    received = True
                    # A stream that yields anything proves Nest accepts the
                    # shared session, so clear auth failures from any transport.
                    sm.record_auth_success()

                if isinstance(update, ProtobufStructureUpdate):
                    _apply_protobuf_structure_update(hass, entry_data, update)
                else:
                    _apply_protobuf_device_update(hass, entry, entry_data, update)

            delay = _next_observe_delay(delay, hass.loop.time() - started)
            LOGGER.debug("Protobuf observe stream ended. Reconnecting in %ds.", delay)
            await asyncio.sleep(delay)

        except NotAuthenticatedException:
            LOGGER.debug("Protobuf observe: 401 exception.")
            sm.record_auth_failure()

            if sm.should_trigger_reauth:
                LOGGER.warning(
                    "Protobuf observe: %d consecutive auth failures, triggering re-authentication",
                    sm.consecutive_auth_failures,
                )
                entry.async_start_reauth(hass)
                return

            await asyncio.sleep(sm.backoff_interval)
            if not await _async_recover_session(hass, entry, entry_data):
                return

        except BadCredentialsException:
            LOGGER.warning(
                "Bad credentials detected. Please re-authenticate the Nest Protect integration."
            )
            entry.async_start_reauth(hass)
            return

        except asyncio.CancelledError:
            LOGGER.debug("Protobuf observe: task cancelled, stopping.")
            raise

        except (httpx.HTTPError, TimeoutError, OSError) as err:
            # Read timeouts, resets and gateway hiccups are routine on a stream
            # that is meant to stay open for weeks. Logging a traceback for each
            # would bury the failures that actually need attention.
            delay = _next_observe_delay(delay, hass.loop.time() - started)
            LOGGER.debug(
                "Protobuf observe: transport error (%r). Reconnecting in %ds.",
                err,
                delay,
            )
            await asyncio.sleep(delay)

        except Exception:  # pylint: disable=broad-except
            delay = _next_observe_delay(delay, hass.loop.time() - started)
            LOGGER.exception("Protobuf observe failed. Updates paused for %ds.", delay)
            await asyncio.sleep(delay)


def _apply_protobuf_structure_update(
    hass: HomeAssistant,
    entry_data: HomeAssistantNestProtectData,
    update: ProtobufStructureUpdate,
) -> None:
    """Merge a protobuf structure update into the legacy structure bucket."""
    if entry_data.protobuf_structure_map is None:
        entry_data.protobuf_structure_map = {}

    if update.legacy_structure_id:
        entry_data.protobuf_structure_map[update.resource_id] = (
            update.legacy_structure_id
        )

    legacy_structure_id = (
        update.legacy_structure_id
        or entry_data.protobuf_structure_map.get(update.resource_id)
    )
    if not legacy_structure_id:
        LOGGER.debug(
            "Protobuf observe: no legacy structure mapping for %s", update.resource_id
        )
        return

    key = f"structure.{legacy_structure_id}"
    structure = entry_data.structures.get(key)
    if not structure:
        LOGGER.debug("Protobuf observe: unknown legacy structure %s", key)
        return

    structure.value["new_structure_id"] = update.resource_id.removeprefix("STRUCTURE_")
    structure.value["using_protobuf"] = True
    if update.user_id:
        structure.value["user_id"] = update.user_id
    if update.away is not None:
        structure.value["away"] = update.away
        structure.value["protobuf_away"] = update.away

    LOGGER.debug(
        "Protobuf observe: updated structure %s with %s",
        key,
        sorted(
            k for k in ("new_structure_id", "user_id", "away") if k in structure.value
        ),
    )
    async_dispatcher_send(hass, key, structure)


async def _async_restore_protobuf_thermostats(
    entry_data: HomeAssistantNestProtectData,
) -> None:
    """Recreate thermostat buckets cached by a previous run.

    Thermostats exist only on the protobuf stream, so without this the sensor
    platform has nothing to add at setup: Home Assistant remembers the entities
    from the registry and shows them as unavailable until the stream reconnects
    and republishes every trait.
    """
    if entry_data.device_store is None:
        return

    cached = await entry_data.device_store.async_load()

    for object_key, value in (cached or {}).items():
        if not object_key.startswith(THERMOSTAT_BUCKET_PREFIX):
            continue

        bucket = Bucket(
            object_key=object_key,
            object_revision=0,
            object_timestamp=0,
            value=dict(value),
        )
        if not is_discoverable_thermostat(bucket):
            continue

        entry_data.devices[object_key] = bucket
        # Already known to the platform, so later trait updates go straight to
        # the per-bucket signal instead of announcing a second discovery.
        entry_data.protobuf_thermostats.add(object_key)

    if entry_data.protobuf_thermostats:
        LOGGER.debug(
            "Restored cached thermostats: %s",
            sorted(entry_data.protobuf_thermostats),
        )


def _seed_protobuf_observe_state(entry_data: HomeAssistantNestProtectData) -> None:
    """Tell the observe decoder about devices we already know.

    The decoder can only route a device trait once `PeerDevicesTrait` has told
    it what type that device is, and the gateway gives no ordering guarantee
    between the two. Everything already known from app_launch or the device
    cache is therefore pushed in up front, so the first stream after a restart
    doesn't have to win that race before it can accept a reading.
    """
    state = entry_data.client.protobuf_observe_state

    for object_key, bucket in entry_data.devices.items():
        device_id = bucket.value.get("device_id")
        if not device_id:
            # Legacy REST buckets are keyed `<type>.<device id>`.
            _, _, device_id = object_key.partition(".")
        if not device_id:
            continue

        if device_type := bucket.value.get("protobuf_device_type"):
            state.device_types[device_id] = device_type
        elif object_key.startswith("kryptonite."):
            # A kryptonite bucket is a Nest Temperature Sensor by definition.
            state.device_types[device_id] = NEST_KRYPTONITE_RESOURCE
        elif object_key.startswith("topaz."):
            # Protects are served over REST here; drop their protobuf traits on
            # sight rather than holding them for a mapping that never comes.
            state.unsupported_devices.add(device_id)

    LOGGER.debug(
        "Seeded observe decoder with %d known device(s)", len(state.device_types)
    )


def _save_protobuf_thermostats(entry_data: HomeAssistantNestProtectData) -> None:
    """Persist thermostat identity, debounced past the initial trait burst."""
    if entry_data.device_store is None:
        return

    entry_data.device_store.async_delay_save(
        lambda: {
            object_key: thermostat_cache_entry(bucket)
            for object_key, bucket in entry_data.devices.items()
            if object_key.startswith(THERMOSTAT_BUCKET_PREFIX)
        },
        DEVICE_CACHE_SAVE_DELAY,
    )


def _apply_protobuf_device_update(
    hass: HomeAssistant,
    entry: ConfigEntry,
    entry_data: HomeAssistantNestProtectData,
    update: ProtobufDeviceUpdate,
) -> None:
    """Merge a protobuf device update into the matching device bucket."""
    device = entry_data.devices.get(update.object_key)
    if not device:
        # Thermostats have no legacy bucket to merge into, so the protobuf
        # stream is the only place they exist. Everything else is expected to
        # come from app_launch first.
        if not update.object_key.startswith(THERMOSTAT_BUCKET_PREFIX):
            LOGGER.debug("Protobuf observe: unknown device %s", update.object_key)
            return

        device = Bucket(
            object_key=update.object_key,
            object_revision=0,
            object_timestamp=0,
            value=dict(update.value),
        )
        entry_data.devices[update.object_key] = device
        LOGGER.debug(
            "Protobuf observe: discovered thermostat %s (%s)",
            update.object_key,
            update.value.get("protobuf_device_type"),
        )
    else:
        device.value.update(update.value)
        LOGGER.debug(
            "Protobuf observe: updated device %s with %s",
            update.object_key,
            sorted(update.value),
        )

    if update.object_key.startswith(THERMOSTAT_BUCKET_PREFIX):
        if not THERMOSTAT_CACHE_KEYS.isdisjoint(update.value):
            _save_protobuf_thermostats(entry_data)

        if update.object_key in entry_data.protobuf_thermostats:
            async_dispatcher_send(hass, update.object_key, device)
        elif is_discoverable_thermostat(device):
            entry_data.protobuf_thermostats.add(update.object_key)
            async_dispatcher_send(
                hass, thermostat_discovery_signal(entry.entry_id), device
            )
        return

    async_dispatcher_send(hass, update.object_key, device)


async def _async_resubscribe(
    hass: HomeAssistant,
    entry: ConfigEntry,
    entry_data: HomeAssistantNestProtectData,
    data: FirstDataAPIResponse,
    started: float,
) -> None:
    """Re-subscribe after a connection error, backing off when errors come fast.

    A long-poll ending in a timeout or disconnect is the normal cycle and
    re-subscribes straight away. An attempt that fails quickly (offline, DNS,
    connection reset, a failing Google refresh) would otherwise retry in a
    tight loop, spinning the CPU and hammering Google or Nest.
    """
    if time.monotonic() - started >= SUBSCRIBE_HEALTHY_SECONDS:
        entry_data.connection_failures = 0
        # A long-poll that ran its course breaks any run of instant answers.
        entry_data.fast_data_responses = 0
    else:
        entry_data.connection_failures += 1
        idx = min(entry_data.connection_failures, len(RECONNECT_INTERVALS)) - 1
        # Jitter keeps installs that lost connectivity together from
        # reconnecting in lockstep.
        delay = RECONNECT_INTERVALS[idx] * random.uniform(0.8, 1.2)  # noqa: S311
        LOGGER.debug(
            "Subscriber: connection failing, retrying in %.0fs (attempt %d)",
            delay,
            entry_data.connection_failures,
        )
        await asyncio.sleep(delay)

    _register_subscribe_task(hass, entry, data)


async def _async_subscribe_for_data(
    hass: HomeAssistant, entry: NestProtectConfigEntry, data: FirstDataAPIResponse
):
    """Subscribe for new data."""
    if not _is_loaded(entry):
        return

    entry_data = entry.runtime_data
    sm = entry_data.session_manager
    started = time.monotonic()

    try:
        await asyncio.sleep(0)

        await sm.ensure_session()

        try:
            result = await entry_data.client.subscribe_for_data(
                entry_data.client.nest_session.access_token,
                entry_data.client.nest_session.userid,
                entry_data.client.transport_url,
                data.updated_buckets,
            )
        except ConnectionTimeoutError:
            # Subclass of TimeoutError, but an unreachable host proves nothing.
            raise
        except TimeoutError:
            # Only a long-poll running out its timeout proves Nest is reachable;
            # a timed-out session refresh above must not reset the deadline.
            entry_data.stream_health.async_record_response()
            raise

        sm.record_auth_success()
        entry_data.connection_failures = 0
        entry_data.stream_health.async_record_response()

        devices_changed = False

        # TODO write this data away in a better way, best would be to directly model API responses in client
        for bucket in result["objects"]:
            key = bucket["object_key"]

            # Bucket list, used to detect devices added to or removed from the account
            if key.startswith(f"{BucketType.BUCKETS}."):
                listed = _listed_device_keys(bucket["value"])
                if listed is not None and listed != entry_data.listed_device_keys:
                    devices_changed = True

            # Nest Protect
            if key.startswith("topaz."):
                topaz = TopazBucket(**bucket)
                entry_data.devices[key] = topaz

                # TODO investigate if we want to use dispatcher, or get data from entry data in sensors
                async_dispatcher_send(hass, key, topaz)

            # Areas
            if key.startswith("where."):
                bucket_value = Bucket(**bucket).value
                for area in bucket_value.wheres:
                    entry_data.areas[area.where_id] = area.name

            # Temperature Sensors
            if key.startswith("kryptonite."):
                kryptonite = Bucket(**bucket)
                entry_data.devices[key] = kryptonite

                async_dispatcher_send(hass, key, kryptonite)

            # Structures / Home-Away
            if key.startswith("structure."):
                structure = Bucket(**bucket)
                entry_data.structures[key] = structure

                async_dispatcher_send(hass, key, structure)

            # Nest Protect connectivity
            if key.startswith("widget_track."):
                widget_track = Bucket(**bucket)
                entry_data.widget_tracks[key] = widget_track

                async_dispatcher_send(hass, key, widget_track)
            # Home safety summaries
            if key in entry_data.safety_summaries:
                safety_summary = Bucket(**bucket)
                entry_data.safety_summaries[key] = safety_summary

                async_dispatcher_send(hass, key, safety_summary)
        if devices_changed:
            # Setup rebuilds the device list and records the new bucket list,
            # so the reload only repeats if the account changes again.
            LOGGER.debug("Subscriber: devices changed on the account, reloading")
            hass.config_entries.async_schedule_reload(entry.entry_id)
            return

        # Update buckets with new data, to only receive new updates
        buckets = {d["object_key"]: d for d in result["objects"]}

        LOGGER.debug(buckets)

        objects = [
            dict(vars(b), **buckets.get(b.object_key, {})) for b in data.updated_buckets
        ]

        data.updated_buckets = [
            Bucket(
                object_key=bucket["object_key"],
                object_revision=bucket["object_revision"],
                object_timestamp=bucket["object_timestamp"],
                value=bucket["value"],
                type=bucket["type"],
            )
            for bucket in objects
        ]

        if time.monotonic() - started < SUBSCRIBE_MIN_INTERVAL_SECONDS:
            entry_data.fast_data_responses += 1
        else:
            entry_data.fast_data_responses = 0

        sm.record_success()

        if entry_data.fast_data_responses > 1:
            if entry_data.fast_data_responses == SUBSCRIBE_FAST_RESPONSE_WARNING:
                LOGGER.warning(
                    "Subscriber: Nest answered %d subscribes in a row immediately; "
                    "spacing re-subscribes %ds apart to protect the Nest API",
                    entry_data.fast_data_responses,
                    SUBSCRIBE_MIN_INTERVAL_SECONDS,
                )
            await asyncio.sleep(SUBSCRIBE_MIN_INTERVAL_SECONDS)

        _register_subscribe_task(hass, entry, data)
    except ServerDisconnectedError:
        LOGGER.debug("Subscriber: server disconnected.")
        await _async_resubscribe(hass, entry, entry_data, data, started)

    # Subclass of TimeoutError, so it must be caught before the long-poll timeout.
    # Counted apart from the session manager so an outage can't push a later
    # 401 over the reauth threshold.
    except ConnectionTimeoutError:
        entry_data.connection_failures += 1
        idx = min(entry_data.connection_failures, len(BACKOFF_INTERVALS)) - 1
        LOGGER.debug(
            "Subscriber: connection timed out, retrying in %ds (attempt %d).",
            BACKOFF_INTERVALS[idx],
            entry_data.connection_failures,
        )
        await asyncio.sleep(BACKOFF_INTERVALS[idx])
        _register_subscribe_task(hass, entry, data)

    except asyncio.exceptions.TimeoutError:
        LOGGER.debug("Subscriber: session timed out.")
        sm.record_success()
        await _async_resubscribe(hass, entry, entry_data, data, started)

    except ClientConnectorError:
        LOGGER.debug("Subscriber: cannot connect to host.")
        await _async_resubscribe(hass, entry, entry_data, data, started)

    except ClientOSError:
        LOGGER.debug("Subscriber: connection reset.")
        await _async_resubscribe(hass, entry, entry_data, data, started)

    except EmptyResponseException:
        LOGGER.debug("Subscriber: Nest Service sent empty response.")
        # A 200 without a body still proves the long-poll round trip works.
        entry_data.stream_health.async_record_response()
        await _async_resubscribe(hass, entry, entry_data, data, started)

    except NotAuthenticatedException:
        LOGGER.debug("Subscriber: 401 exception.")
        sm.record_auth_failure()

        if sm.should_trigger_reauth:
            LOGGER.warning(
                "Subscriber: %d consecutive auth failures, triggering re-authentication",
                sm.consecutive_auth_failures,
            )
            entry.async_start_reauth(hass)
            return

        LOGGER.debug(
            "Subscriber: retrying in %ds (attempt %d)",
            sm.backoff_interval,
            sm.consecutive_auth_failures,
        )
        await asyncio.sleep(sm.backoff_interval)

        if not await _async_recover_session(hass, entry, entry_data):
            return

        _register_subscribe_task(hass, entry, data)

    except BadCredentialsException:
        LOGGER.warning(
            "Bad credentials detected. Please re-authenticate the Nest Protect integration."
        )
        entry.async_start_reauth(hass)
        return

    except NestServiceException:
        # Also raised when Google throttles the token refresh, so keep backing
        # off rather than retrying at a fixed rate.
        sm.record_error()
        delay = max(SERVICE_ERROR_MIN_SECONDS, sm.error_backoff_interval)
        LOGGER.debug("Subscriber: Nest Service error. Updates paused for %ds.", delay)
        await asyncio.sleep(delay)
        _register_subscribe_task(hass, entry, data)

    except PynestException:
        sm.record_error()
        delay = max(PYNEST_ERROR_MIN_SECONDS, sm.error_backoff_interval)
        LOGGER.exception(
            "Unknown pynest exception. Please create an issue on GitHub with your logfile. Updates paused for %ds.",
            delay,
        )
        await asyncio.sleep(delay)
        _register_subscribe_task(hass, entry, data)

    except asyncio.CancelledError:
        LOGGER.debug("Subscriber: task cancelled, stopping subscription.")
        raise

    except Exception:  # pylint: disable=broad-except
        sm.record_error()
        LOGGER.exception(
            "Unknown exception. Please create an issue on GitHub with your logfile. Updates paused for %ds.",
            sm.error_backoff_interval,
        )
        await asyncio.sleep(sm.error_backoff_interval)
        _register_subscribe_task(hass, entry, data)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: NestProtectConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Only allow removing devices that the account no longer reports."""
    entry_data: HomeAssistantNestProtectData | None = getattr(
        config_entry, "runtime_data", None
    )
    if entry_data is None:
        return True

    return not _is_reported_device(hass, entry_data, device_entry)
