"""Nest Protect integration."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from aiohttp import ClientConnectorError, ClientError, ServerDisconnectedError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
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
    DOMAIN,
    LOGGER,
    MAX_AUTH_FAILURES,
    PLATFORMS,
    SESSION_EXPIRY_BUFFER_SECONDS,
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
    NestResponse,
    TopazBucket,
    WhereBucketValue,
)


@dataclass
class HomeAssistantNestProtectData:
    """Nest Protect data stored in the Home Assistant data object."""

    devices: dict[str, Bucket]
    areas: dict[str, str]
    client: NestClient
    store: Store
    subscription_task: asyncio.Task | None = None
    _consecutive_failures: int = 0


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


async def _async_persist_session(
    store: Store, nest_session: NestResponse, transport_url: str | None
) -> None:
    """Persist Nest session to storage for reuse across restarts."""
    await store.async_save(
        {
            "nest_session": nest_session.to_dict(),
            "transport_url": transport_url,
        }
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Nest Protect from a config entry.

    Authentication strategy (three-tier fallback):
    1. Reuse persisted Nest session if still valid (skip Google entirely)
    2. Re-authenticate with Google using stored cookies
    3. Raise ConfigEntryAuthFailed (user must re-enter credentials)
    """
    issue_token = None
    cookies = None
    refresh_token = None

    if CONF_ISSUE_TOKEN in entry.data and CONF_COOKIES in entry.data:
        issue_token = entry.data[CONF_ISSUE_TOKEN]
        cookies = entry.data[CONF_COOKIES]
    if CONF_REFRESH_TOKEN in entry.data:
        refresh_token = entry.data[CONF_REFRESH_TOKEN]

    session = async_create_clientsession(hass)
    account_type = entry.data.get(CONF_ACCOUNT_TYPE, Environment.PRODUCTION)
    client = NestClient(session=session, environment=NEST_ENVIRONMENTS[account_type])

    # Assign credentials so client.get_access_token() works for later re-auth
    client.issue_token = issue_token
    client.cookies = cookies
    client.refresh_token = refresh_token

    store = Store(
        hass, STORAGE_VERSION, STORAGE_KEY_FORMAT.format(entry_id=entry.entry_id)
    )

    # --- Tier 1: Try reusing persisted Nest session ---
    nest = None
    data = None
    persisted = await store.async_load()

    if persisted and persisted.get("nest_session"):
        restored_session = NestResponse.from_dict(persisted["nest_session"])

        if restored_session and not restored_session.is_expired(
            buffer_seconds=SESSION_EXPIRY_BUFFER_SECONDS
        ):
            LOGGER.debug(
                "Reusing persisted Nest session (expires: %s)",
                restored_session.expires_in,
            )
            client.nest_session = restored_session
            client.transport_url = persisted.get("transport_url")

            # Validate the session is actually accepted by Nest
            try:
                data = await client.get_first_data(
                    restored_session.access_token, restored_session.userid
                )
                nest = restored_session
            except (NotAuthenticatedException, PynestException):
                LOGGER.debug(
                    "Persisted session rejected by Nest, falling through to cookie auth"
                )
                client.nest_session = None
                nest = None
        else:
            LOGGER.debug("Persisted session expired, falling through to cookie auth")

    # --- Tier 2: Re-authenticate with Google cookies ---
    if nest is None:
        try:
            # Using user-retrieved cookies for authentication
            if issue_token and cookies:
                auth = await client.get_access_token_from_cookies(issue_token, cookies)
            # Using refresh_token from legacy authentication method
            elif refresh_token:
                auth = await client.get_access_token_from_refresh_token(refresh_token)
            else:
                raise ConfigEntryAuthFailed("No credentials available")

            nest = await client.authenticate(auth.access_token)
            client.nest_session = nest

            LOGGER.debug(
                "Cookie auth succeeded, cookies refreshed: %s",
                client.refreshed_cookies is not None,
            )

            # Persist the new session for next restart
            await _async_persist_session(store, nest, client.transport_url)

            # Update cookies in config entry if Google returned refreshed cookies
            if client.refreshed_cookies and client.refreshed_cookies != cookies:
                hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_COOKIES: client.refreshed_cookies},
                )

        except (TimeoutError, ClientError) as exception:
            raise ConfigEntryNotReady from exception
        except BadCredentialsException as exception:
            raise ConfigEntryAuthFailed from exception
        except Exception as exception:  # pylint: disable=broad-except
            LOGGER.exception("Unknown exception.")
            raise ConfigEntryNotReady from exception

    if data is None:
        data = await client.get_first_data(nest.access_token, nest.userid)

    device_buckets: list[Bucket] = []
    areas: dict[str, str] = {}

    for bucket in data.updated_buckets:
        # Nest Protect and Temperature Sensors
        if bucket.type in {BucketType.TOPAZ, BucketType.KRYPTONITE}:
            device_buckets.append(bucket)

        # Areas
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
        store=store,
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Subscribe for real-time updates
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
    """Subscribe for new data.

    Uses exponential backoff on repeated failures and raises
    ConfigEntryAuthFailed after MAX_AUTH_FAILURES consecutive auth errors.
    """
    # Check if entry is still loaded
    if entry.entry_id not in hass.data.get(DOMAIN, {}):
        return

    entry_data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]

    try:
        # Check for cancellation early to avoid creating orphaned tasks
        # if the entry is being unloaded
        await asyncio.sleep(0)

        # TODO move refresh token logic to client
        if (
            not entry_data.client.nest_session
            or entry_data.client.nest_session.is_expired(
                buffer_seconds=SESSION_EXPIRY_BUFFER_SECONDS
            )
        ):
            LOGGER.debug("Subscriber: authenticate for new Nest session")

            if not entry_data.client.auth or entry_data.client.auth.is_expired():
                LOGGER.debug("Subscriber: retrieving new Google access token")
                await entry_data.client.get_access_token()

            if entry_data.client.auth:
                entry_data.client.nest_session = (
                    await entry_data.client.authenticate(
                        entry_data.client.auth.access_token
                    )
                )

                # Persist refreshed session for next restart
                await _async_persist_session(
                    entry_data.store,
                    entry_data.client.nest_session,
                    entry_data.client.transport_url,
                )

        # Subscribe to Google Nest subscribe endpoint
        result = await entry_data.client.subscribe_for_data(
            entry_data.client.nest_session.access_token,
            entry_data.client.nest_session.userid,
            data.service_urls["urls"]["transport_url"],
            data.updated_buckets,
        )

        # Reset failure counter on success
        entry_data._consecutive_failures = 0

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

    except EmptyResponseException:
        LOGGER.debug("Subscriber: Nest Service sent empty response.")
        _register_subscribe_task(hass, entry, data)

    except NotAuthenticatedException:
        LOGGER.debug("Subscriber: 401 exception.")
        # Renewing access token
        entry_data._consecutive_failures += 1

        if entry_data._consecutive_failures >= MAX_AUTH_FAILURES:
            LOGGER.warning(
                "Subscriber: %d consecutive auth failures, triggering re-authentication",
                entry_data._consecutive_failures,
            )
            entry.async_start_reauth(hass)
            return

        backoff = BACKOFF_INTERVALS[
            min(entry_data._consecutive_failures - 1, len(BACKOFF_INTERVALS) - 1)
        ]
        LOGGER.debug(
            "Subscriber: retrying in %ds (attempt %d)",
            backoff,
            entry_data._consecutive_failures,
        )
        await asyncio.sleep(backoff)

        await entry_data.client.get_access_token()
        await entry_data.client.authenticate(entry_data.client.auth.access_token)

        if entry_data.client.nest_session:
            await _async_persist_session(
                entry_data.store,
                entry_data.client.nest_session,
                entry_data.client.transport_url,
            )

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

        # Wait a minute before retrying
        await asyncio.sleep(60)
        _register_subscribe_task(hass, entry, data)

    except asyncio.CancelledError:
        # Task is being cancelled during unload; do not register a new task
        LOGGER.debug("Subscriber: task cancelled, stopping subscription.")
        raise

    except Exception:  # pylint: disable=broad-except
        entry_data._consecutive_failures += 1
        backoff = BACKOFF_INTERVALS[
            min(entry_data._consecutive_failures - 1, len(BACKOFF_INTERVALS) - 1)
        ]

        LOGGER.exception(
            "Unknown exception. Please create an issue on GitHub with your logfile. Updates paused for %ds.",
            backoff,
        )

        await asyncio.sleep(backoff)
        _register_subscribe_task(hass, entry, data)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Remove a config entry from a device."""
    return True
