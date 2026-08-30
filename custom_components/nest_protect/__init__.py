"""Nest Protect integration."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from aiohttp import (
    ClientConnectorError,
    ClientError,
    ClientOSError,
    ServerDisconnectedError,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import (
    CONF_ACCOUNT_TYPE,
    CONF_COOKIES,
    CONF_ISSUE_TOKEN,
    CONF_REFRESH_TOKEN,
    DEVICE_CACHE_SAVE_DELAY,
    DOMAIN,
    LOGGER,
    PLATFORMS,
    STORAGE_KEY_DEVICES_FORMAT,
    STORAGE_KEY_FORMAT,
    STORAGE_VERSION,
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
from .pynest.protobuf import ProtobufDeviceUpdate, ProtobufStructureUpdate
from .session import NestSessionManager
from .thermostat import (
    THERMOSTAT_BUCKET_PREFIX,
    THERMOSTAT_CACHE_KEYS,
    is_discoverable_thermostat,
    thermostat_cache_entry,
    thermostat_discovery_signal,
)


@dataclass
class HomeAssistantNestProtectData:
    """Nest Protect data stored in the Home Assistant data object."""

    devices: dict[str, Bucket]
    structures: dict[str, Bucket]
    areas: dict[str, str]
    client: NestClient
    session_manager: NestSessionManager
    grpc_lock_client: GrpcLockClient
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


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    """Migrate old Config entries."""
    LOGGER.debug("Migrating from version %s", config_entry.version)

    if config_entry.version == 1:
        hass.config_entries.async_update_entry(
            config_entry,
            data={**config_entry.data, CONF_ACCOUNT_TYPE: Environment.PRODUCTION},
            version=2,
        )

    LOGGER.debug("Migration to version %s successful", config_entry.version)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
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

    session_manager = NestSessionManager(client=client, store=store)

    try:
        data = await session_manager.async_setup()
    except (TimeoutError, ClientError) as exception:
        raise ConfigEntryNotReady from exception
    except BadCredentialsException as exception:
        raise ConfigEntryAuthFailed from exception
    except Exception as exception:  # pylint: disable=broad-except
        LOGGER.exception("Unknown exception.")
        raise ConfigEntryNotReady from exception

    if data is None:
        raise ConfigEntryAuthFailed("No credentials available")

    # Update cookies in config entry if Google returned refreshed ones
    _persist_refreshed_cookies(hass, entry, client, session_manager)

    device_buckets: list[Bucket] = []
    structure_buckets: list[Bucket] = []
    areas: dict[str, str] = {}

    for bucket in data.updated_buckets:
        if bucket.type in {BucketType.TOPAZ, BucketType.KRYPTONITE}:
            device_buckets.append(bucket)

        if bucket.type == BucketType.STRUCTURE:
            structure_buckets.append(bucket)

        if bucket.type == BucketType.WHERE and isinstance(
            bucket.value, WhereBucketValue
        ):
            bucket_value = bucket.value
            for area in bucket_value.wheres:
                areas[area.where_id] = area.name

    devices: dict[str, Bucket] = {b.object_key: b for b in device_buckets}
    structures: dict[str, Bucket] = {b.object_key: b for b in structure_buckets}

    entry_data = HomeAssistantNestProtectData(
        devices=devices,
        structures=structures,
        areas=areas,
        client=client,
        session_manager=session_manager,
        grpc_lock_client=GrpcLockClient(client),
        device_store=Store(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY_DEVICES_FORMAT.format(entry_id=entry.entry_id),
        ),
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data

    await _async_restore_protobuf_thermostats(entry_data)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry_data.subscription_task = asyncio.create_task(
        _async_subscribe_for_data(hass, entry, data)
    )
    entry_data.protobuf_observe_task = asyncio.create_task(
        _async_observe_for_protobuf_data(hass, entry)
    )

    entry_data.lock_observe_task = entry.async_create_background_task(
        hass,
        _async_observe_locks_loop(hass, entry),
        name=f"{DOMAIN}_lock_observe_{entry.entry_id}",
    )

    return True


async def _async_observe_locks_loop(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Consume the gRPC observe stream and dispatch lock updates.

    Returns — ending the background task — when the observer reports that this
    account has no locks, so accounts without a Nest x Yale lock stop talking to
    the gateway after the first stream. Auth failures are retried with a session
    refresh, and escalate to re-auth after MAX_AUTH_FAILURES.

    Auth health is tracked on the shared NestSessionManager rather than locally,
    so this loop and the REST subscriber can't each sit below the threshold
    while the session is thoroughly broken.
    """
    entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    cache = entry_data.lock_state_cache
    sm = entry_data.session_manager

    while True:
        try:
            async for batch in entry_data.grpc_lock_client.observe_locks():
                # A working stream clears failures recorded by either transport.
                sm.record_success()
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
            sm.record_failure()
            if sm.should_trigger_reauth:
                LOGGER.warning(
                    "Lock observer: %d consecutive auth failures, triggering "
                    "re-authentication",
                    sm.consecutive_failures,
                )
                entry.async_start_reauth(hass)
                return

            LOGGER.debug(
                "Lock observer: credentials rejected (%r), refreshing session", err
            )
            await asyncio.sleep(sm.backoff_interval)
            await sm.async_refresh_session()

            # Entry may have been unloaded during the backoff sleep
            if entry.entry_id not in hass.data.get(DOMAIN, {}):
                return

            _persist_refreshed_cookies(hass, entry, entry_data.client, sm)
        except Exception:
            LOGGER.exception("Lock observe loop failed unexpectedly")
            return
        else:
            # observe_locks() only ends the iteration when it has concluded
            # there is nothing to watch on this account.
            LOGGER.debug("Lock observer: no locks on this account, stopping")
            return


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Cancel background tasks only after successful platform unload
        if entry.entry_id in hass.data.get(DOMAIN, {}):
            entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
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
            hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up persisted session and device data when the entry is removed."""
    for key_format in (STORAGE_KEY_FORMAT, STORAGE_KEY_DEVICES_FORMAT):
        store = Store(hass, STORAGE_VERSION, key_format.format(entry_id=entry.entry_id))
        await store.async_remove()


def _persist_refreshed_cookies(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: NestClient,
    sm: NestSessionManager,
) -> None:
    """Persist Google-rotated cookies back to the config entry and client.

    Google may rotate OAuth cookies during ``get_access_token_from_cookies``.
    Without writing them back, a HA restart would use stale cookies and force
    re-authentication; the in-memory client also needs the update so the next
    refresh in the same HA session uses fresh cookies.
    """
    new_cookies = sm.refreshed_cookies
    if not new_cookies or new_cookies == entry.data.get(CONF_COOKIES):
        return

    LOGGER.debug("Persisting refreshed Nest cookies")
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_COOKIES: new_cookies},
    )
    client.cookies = new_cookies


def _register_subscribe_task(
    hass: HomeAssistant, entry: ConfigEntry, data: FirstDataAPIResponse
) -> asyncio.Task | None:
    """Create a new subscription task and update the reference."""
    # Check if entry is still loaded before creating new task
    if entry.entry_id not in hass.data.get(DOMAIN, {}):
        return None

    entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    task = asyncio.create_task(_async_subscribe_for_data(hass, entry, data))
    entry_data.subscription_task = task
    return task


async def _async_observe_for_protobuf_data(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Observe protobuf data used by Home/Away on migrated Nest accounts."""
    while entry.entry_id in hass.data.get(DOMAIN, {}):
        entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
        sm = entry_data.session_manager

        try:
            await sm.ensure_session()

            async for update in entry_data.client.observe_for_structure_updates(
                entry_data.client.nest_session.access_token,
            ):
                if isinstance(update, ProtobufStructureUpdate):
                    _apply_protobuf_structure_update(hass, entry_data, update)
                else:
                    _apply_protobuf_device_update(hass, entry, entry_data, update)

            LOGGER.debug("Protobuf observe stream ended.")
            await asyncio.sleep(5)

        except NotAuthenticatedException:
            LOGGER.debug("Protobuf observe: 401 exception.")
            sm.record_failure()

            if sm.should_trigger_reauth:
                LOGGER.warning(
                    "Protobuf observe: %d consecutive auth failures, triggering re-authentication",
                    sm.consecutive_failures,
                )
                entry.async_start_reauth(hass)
                return

            await asyncio.sleep(sm.backoff_interval)
            await sm.async_refresh_session()

        except BadCredentialsException:
            LOGGER.warning(
                "Bad credentials detected. Please re-authenticate the Nest Protect integration."
            )
            entry.async_start_reauth(hass)
            return

        except asyncio.CancelledError:
            LOGGER.debug("Protobuf observe: task cancelled, stopping.")
            raise

        except Exception:  # pylint: disable=broad-except
            LOGGER.exception(
                "Protobuf observe failed. Updates paused for %ds.",
                sm.backoff_interval,
            )
            await asyncio.sleep(sm.backoff_interval)


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


async def _async_subscribe_for_data(
    hass: HomeAssistant, entry: ConfigEntry, data: FirstDataAPIResponse
):
    """Subscribe for new data."""
    if entry.entry_id not in hass.data.get(DOMAIN, {}):
        return

    entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    sm = entry_data.session_manager

    try:
        await asyncio.sleep(0)

        await sm.ensure_session()
        _persist_refreshed_cookies(hass, entry, entry_data.client, sm)

        result = await entry_data.client.subscribe_for_data(
            entry_data.client.nest_session.access_token,
            entry_data.client.nest_session.userid,
            data.service_urls["urls"]["transport_url"],
            data.updated_buckets,
        )

        sm.record_success()

        # TODO write this data away in a better way, best would be to directly model API responses in client
        for bucket in result["objects"]:
            key = bucket["object_key"]

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

        _register_subscribe_task(hass, entry, data)
    except ServerDisconnectedError:
        LOGGER.debug("Subscriber: server disconnected.")
        _register_subscribe_task(hass, entry, data)

    except asyncio.exceptions.TimeoutError:
        LOGGER.debug("Subscriber: session timed out.")
        sm.record_success()
        _register_subscribe_task(hass, entry, data)

    except ClientConnectorError:
        LOGGER.debug("Subscriber: cannot connect to host.")
        _register_subscribe_task(hass, entry, data)

    except ClientOSError:
        LOGGER.debug("Subscriber: connection reset.")
        _register_subscribe_task(hass, entry, data)

    except EmptyResponseException:
        LOGGER.debug("Subscriber: Nest Service sent empty response.")
        _register_subscribe_task(hass, entry, data)

    except NotAuthenticatedException:
        LOGGER.debug("Subscriber: 401 exception.")
        sm.record_failure()

        if sm.should_trigger_reauth:
            LOGGER.warning(
                "Subscriber: %d consecutive auth failures, triggering re-authentication",
                sm.consecutive_failures,
            )
            entry.async_start_reauth(hass)
            return

        LOGGER.debug(
            "Subscriber: retrying in %ds (attempt %d)",
            sm.backoff_interval,
            sm.consecutive_failures,
        )
        await asyncio.sleep(sm.backoff_interval)

        await sm.async_refresh_session()

        # Entry may have been unloaded during the backoff sleep
        if entry.entry_id not in hass.data.get(DOMAIN, {}):
            return

        _persist_refreshed_cookies(hass, entry, entry_data.client, sm)

        _register_subscribe_task(hass, entry, data)

    except BadCredentialsException:
        LOGGER.warning(
            "Bad credentials detected. Please re-authenticate the Nest Protect integration."
        )
        entry.async_start_reauth(hass)
        return

    except NestServiceException:
        LOGGER.debug("Subscriber: Nest Service error. Updates paused for 2 minutes.")
        await asyncio.sleep(60 * 2)
        _register_subscribe_task(hass, entry, data)

    except PynestException:
        LOGGER.exception(
            "Unknown pynest exception. Please create an issue on GitHub with your logfile. Updates paused for 1 minute."
        )
        await asyncio.sleep(60)
        _register_subscribe_task(hass, entry, data)

    except asyncio.CancelledError:
        LOGGER.debug("Subscriber: task cancelled, stopping subscription.")
        raise

    except Exception:  # pylint: disable=broad-except
        sm.record_failure()
        LOGGER.exception(
            "Unknown exception. Please create an issue on GitHub with your logfile. Updates paused for %ds.",
            sm.backoff_interval,
        )
        await asyncio.sleep(sm.backoff_interval)
        _register_subscribe_task(hass, entry, data)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Remove a config entry from a device."""
    return True
