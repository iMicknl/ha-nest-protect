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
from homeassistant.helpers import issue_registry as ir
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
    ISSUE_COOKIE_EXPIRED,
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
    reauth_pending: bool = False


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

    # Keep Google cookies alive even when tier-1 persisted Nest session succeeded
    await session_manager.async_refresh_google_cookies()

    # Update credentials in config entry if Google returned refreshed ones
    _persist_refreshed_auth(hass, entry, client, session_manager)

    if session_manager.cookie_auth_failed:
        LOGGER.warning(
            "Google cookie refresh failed on startup; re-authentication recommended"
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

    if session_manager.cookie_auth_failed:
        _maybe_request_reauth(hass, entry, entry_data)

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

    _clear_cookie_expired_issue(hass, entry)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up persisted session data when the config entry is removed."""
    store = Store(
        hass, STORAGE_VERSION, STORAGE_KEY_FORMAT.format(entry_id=entry.entry_id)
    )
    await store.async_remove()
    _clear_cookie_expired_issue(hass, entry)


def _cookie_expired_issue_id(entry: ConfigEntry) -> str:
    return f"{ISSUE_COOKIE_EXPIRED}_{entry.entry_id}"


def _create_cookie_expired_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create a repair issue when Google cookies have expired."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _cookie_expired_issue_id(entry),
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_COOKIE_EXPIRED,
        translation_placeholders={"title": entry.title},
        data={"entry_id": entry.entry_id},
    )


def _clear_cookie_expired_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the cookie-expired repair issue when auth recovers."""
    ir.async_delete_issue(hass, DOMAIN, _cookie_expired_issue_id(entry))


def _maybe_request_reauth(
    hass: HomeAssistant, entry: ConfigEntry, entry_data: HomeAssistantNestProtectData
) -> None:
    """Request re-authentication once until cookies recover."""
    if entry_data.reauth_pending:
        return

    entry_data.reauth_pending = True
    _create_cookie_expired_issue(hass, entry)
    entry.async_start_reauth(hass)


def _persist_refreshed_auth(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: NestClient,
    sm: NestSessionManager,
) -> None:
    """Persist Google-rotated credentials back to the config entry and client.

    Google may rotate OAuth cookies, issue_token URL, or (rarely) return a
    refresh_token during ``get_access_token_from_cookies``. Without writing them
    back, a HA restart would use stale credentials.
    """
    new_cookies = sm.refreshed_cookies
    new_issue_token = sm.refreshed_issue_token
    new_refresh_token = sm.refreshed_refresh_token

    updates: dict[str, str] = {}
    if new_cookies and new_cookies != entry.data.get(CONF_COOKIES):
        updates[CONF_COOKIES] = new_cookies
        client.cookies = new_cookies
    if new_issue_token and new_issue_token != entry.data.get(CONF_ISSUE_TOKEN):
        updates[CONF_ISSUE_TOKEN] = new_issue_token
        client.issue_token = new_issue_token
    if new_refresh_token and new_refresh_token != entry.data.get(CONF_REFRESH_TOKEN):
        updates[CONF_REFRESH_TOKEN] = new_refresh_token
        client.refresh_token = new_refresh_token
        LOGGER.info("Persisted legacy refresh_token from issueToken response")

    if not updates:
        return

    LOGGER.debug("Persisting refreshed Nest auth credentials: %s", sorted(updates))
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, **updates},
    )


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

        if sm.cookie_auth_failed:
            LOGGER.warning(
                "Google cookies expired; re-authentication needed. "
                "Continuing with current Nest session until it expires."
            )
            _maybe_request_reauth(hass, entry, entry_data)

        _persist_refreshed_auth(hass, entry, entry_data.client, sm)

        if not entry_data.client.nest_session:
            await asyncio.sleep(sm.backoff_interval)
            _register_subscribe_task(hass, entry, data)
            return

        result = await entry_data.client.subscribe_for_data(
            entry_data.client.nest_session.access_token,
            entry_data.client.nest_session.userid,
            data.service_urls["urls"]["transport_url"],
            data.updated_buckets,
        )

        sm.record_success()
        entry_data.reauth_pending = False
        _clear_cookie_expired_issue(hass, entry)

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

        _persist_refreshed_auth(hass, entry, entry_data.client, sm)

        _register_subscribe_task(hass, entry, data)

    except BadCredentialsException:
        LOGGER.warning(
            "Bad credentials detected. Please re-authenticate the Nest Protect integration."
        )
        _maybe_request_reauth(hass, entry, entry_data)
        if entry_data.client.nest_session:
            _register_subscribe_task(hass, entry, data)
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
