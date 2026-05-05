"""Nest Protect integration."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

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
    DOMAIN,
    LOGGER,
    PLATFORMS,
    STORAGE_KEY_FORMAT,
    STORAGE_VERSION,
)
from .pynest.client import NestClient
from .pynest.const import NEST_ENVIRONMENTS
from .pynest.enums import BucketType, Environment
from .pynest.exceptions import (
    BadCredentialsException,
    EmptyResponseException,
    NestServiceException,
    NotAuthenticatedException,
    PynestException,
)
from .pynest.models import (
    Bucket,
    FirstDataAPIResponse,
    TopazBucket,
    WhereBucketValue,
)
from .session import NestSessionManager


@dataclass
class HomeAssistantNestProtectData:
    """Nest Protect data stored in the Home Assistant data object."""

    devices: dict[str, Bucket]
    areas: dict[str, str]
    client: NestClient
    session_manager: NestSessionManager
    subscription_task: asyncio.Task | None = None


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
    if (
        session_manager.refreshed_cookies
        and session_manager.refreshed_cookies != cookies
    ):
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_COOKIES: session_manager.refreshed_cookies},
        )

    device_buckets: list[Bucket] = []
    areas: dict[str, str] = {}

    for bucket in data.updated_buckets:
        if bucket.type in {BucketType.TOPAZ, BucketType.KRYPTONITE}:
            device_buckets.append(bucket)

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
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry_data.subscription_task = asyncio.create_task(
        _async_subscribe_for_data(hass, entry, data)
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Cancel subscription task only after successful platform unload
        if entry.entry_id in hass.data.get(DOMAIN, {}):
            entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
            if entry_data.subscription_task:
                entry_data.subscription_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await entry_data.subscription_task
            hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up persisted session data when the config entry is removed."""
    store = Store(
        hass, STORAGE_VERSION, STORAGE_KEY_FORMAT.format(entry_id=entry.entry_id)
    )
    await store.async_remove()


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
