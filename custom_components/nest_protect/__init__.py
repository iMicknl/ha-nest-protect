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
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import (
    CONF_ACCOUNT_TYPE,
    CONF_COOKIES,
    CONF_ISSUE_TOKEN,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    LOGGER,
    PLATFORMS,
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
from .session import NestSessionManager

DEVICE_BUCKET_TYPES = {BucketType.TOPAZ, BucketType.KRYPTONITE}


@dataclass
class HomeAssistantNestProtectData:
    """Nest Protect data stored in the Home Assistant data object."""

    devices: dict[str, Bucket]
    areas: dict[str, str]
    client: NestClient
    session_manager: NestSessionManager
    grpc_lock_client: GrpcLockClient
    subscription_task: asyncio.Task | None = None
    lock_observe_task: asyncio.Task | None = None
    lock_state_cache: dict[str, LockState] = field(default_factory=dict)
    # Device keys from the account's bucket list at setup; None if not returned.
    listed_device_keys: set[str] | None = None


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
    areas: dict[str, str] = {}
    listed_device_keys: set[str] | None = None

    for bucket in data.updated_buckets:
        if bucket.type in DEVICE_BUCKET_TYPES:
            device_buckets.append(bucket)

        if bucket.type == BucketType.BUCKETS:
            listed_device_keys = _listed_device_keys(bucket.value)

        if bucket.type == BucketType.WHERE and isinstance(
            bucket.value, WhereBucketValue
        ):
            bucket_value = bucket.value
            for area in bucket_value.wheres:
                areas[area.where_id] = area.name

    devices: dict[str, Bucket] = {b.object_key: b for b in device_buckets}

    entry_data = HomeAssistantNestProtectData(
        devices=devices,
        areas=areas,
        client=client,
        session_manager=session_manager,
        grpc_lock_client=GrpcLockClient(client),
        listed_device_keys=listed_device_keys,
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data

    _async_remove_stale_devices(hass, entry, entry_data)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry_data.subscription_task = asyncio.create_task(
        _async_subscribe_for_data(hass, entry, data)
    )

    entry_data.lock_observe_task = entry.async_create_background_task(
        hass,
        _async_observe_locks_loop(hass, entry),
        name=f"{DOMAIN}_lock_observe_{entry.entry_id}",
    )

    return True


def _listed_device_keys(value: dict) -> set[str] | None:
    """Return the Protect and temperature sensor keys from a bucket list."""
    keys = value.get("buckets")
    if not isinstance(keys, list):
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
    return not _device_bucket_keys(hass, device.id).isdisjoint(entry_data.devices)


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
        device_registry.async_update_device(
            device.id, remove_config_entry_id=entry.entry_id
        )


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
            hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up persisted session data when the config entry is removed."""
    store = Store(
        hass, STORAGE_VERSION, STORAGE_KEY_FORMAT.format(entry_id=entry.entry_id)
    )
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


async def _async_recover_session(
    hass: HomeAssistant, entry: ConfigEntry, entry_data: HomeAssistantNestProtectData
) -> bool:
    """Refresh rejected credentials and report whether background work can resume."""
    if entry.entry_id not in hass.data.get(DOMAIN, {}):
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

    if entry.entry_id not in hass.data.get(DOMAIN, {}):
        return False

    _persist_refreshed_cookies(
        hass, entry, entry_data.client, entry_data.session_manager
    )
    return True


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
    """Only allow removing devices that the account no longer reports."""
    entry_data: HomeAssistantNestProtectData | None = hass.data.get(DOMAIN, {}).get(
        config_entry.entry_id
    )
    if entry_data is None:
        return True

    return not _is_reported_device(hass, entry_data, device_entry)
