"""Provides diagnostics for Nest Protect."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import HomeAssistantNestProtectData, NestProtectConfigEntry
from .const import CONF_COOKIES, CONF_ISSUE_TOKEN, CONF_REFRESH_TOKEN, DOMAIN, LOGGER
from .pynest.client import NestClient
from .pynest.const import FULL_NEST_REQUEST
from .pynest.models import FirstDataAPIResponse

TO_REDACT = [
    CONF_COOKIES,
    CONF_ISSUE_TOKEN,
    CONF_REFRESH_TOKEN,
    "access_token",
    "address_lines",
    "aux_primary_fabric_id",
    "city",
    "country",
    "email",
    "emergency_contact_description",
    "emergency_contact_phone",
    "ifj_primary_fabric_id",
    "latitude",
    "location",
    "longitude",
    "mac_address",
    "name",
    "parameters",
    "pairing_token",
    "postal_code",
    "profile_image_url",
    "serial_number",
    "service_config",
    "state",
    "sunrise",
    "sunset",
    "temp_c",
    "thread_ip_address",
    "thread_mac_address",
    "time_zone",
    "topaz_hush_key",
    "user",
    "userid",
    "wifi_ip_address",
    "wifi_mac_address",
    "zip",
]

NO_VALID_SESSION = "no_valid_session"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NestProtectConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data: dict[str, Any] = {"config_entry": dict(entry.data)}

    entry_data: HomeAssistantNestProtectData | None = getattr(
        entry, "runtime_data", None
    )
    if entry_data is None:
        return async_redact_data(data, TO_REDACT)

    app_launch, error = await _async_fetch_app_launch(entry_data)

    data |= {
        # Captured after the fetch, which may have replaced the session.
        "session": _session_info(entry_data),
        "google_refresh": entry_data.session_manager.google_refresh_diagnostics,
        "devices": {
            key: dataclasses.asdict(bucket)
            for key, bucket in entry_data.devices.items()
        },
        "areas": [
            {"where_id": where_id, "name": name}
            for where_id, name in entry_data.areas.items()
        ],
        "locks": [
            dataclasses.asdict(lock) for lock in entry_data.lock_state_cache.values()
        ],
        "app_launch": dataclasses.asdict(app_launch) if app_launch else None,
        "app_launch_error": error,
    }

    return _redact(data, entry_data)


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: NestProtectConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a device entry."""
    data: dict[str, Any] = {
        "device": {
            "model": device.model,
            "sw_version": device.sw_version,
            "hw_version": device.hw_version,
        },
    }

    entry_data: HomeAssistantNestProtectData | None = getattr(
        entry, "runtime_data", None
    )
    if entry_data is None:
        return data

    for lock in entry_data.lock_state_cache.values():
        if (DOMAIN, lock.serial_number) in device.identifiers:
            data["lock"] = dataclasses.asdict(lock)
            return _redact(data, entry_data)

    bucket = next(
        (
            bucket
            for bucket in entry_data.devices.values()
            if (DOMAIN, bucket.value.get("serial_number") or bucket.object_key)
            in device.identifiers
        ),
        None,
    )
    if bucket is None:
        return _redact(data, entry_data)

    data["bucket"] = dataclasses.asdict(bucket)

    # Buckets describing the same device (e.g. widget_track) share its object id.
    object_id = bucket.object_key.partition(".")[2]
    app_launch, error = await _async_fetch_app_launch(entry_data)
    data["related_buckets"] = [
        dataclasses.asdict(related)
        for related in (app_launch.updated_buckets if app_launch else [])
        if related.object_key.partition(".")[2] == object_id
    ]
    data["app_launch_error"] = error

    return _redact(data, entry_data)


def _session_info(entry_data: HomeAssistantNestProtectData) -> dict[str, Any]:
    """Describe the live session without exposing its token."""
    session = entry_data.client.nest_session
    return {
        "expires_in": session.expires_in if session else None,
        "expired": session.is_expired() if session else None,
        "consecutive_auth_failures": entry_data.session_manager.consecutive_auth_failures,
        "consecutive_errors": entry_data.session_manager.consecutive_errors,
        "transport_url": entry_data.client.transport_url,
    }


async def _async_fetch_app_launch(
    entry_data: HomeAssistantNestProtectData,
) -> tuple[FirstDataAPIResponse | None, str | None]:
    """Fetch every bucket type with the live session, without altering the live client.

    The in-memory buckets only cover the types the integration subscribes to,
    so the full app_launch is what makes diagnostics useful for adding support
    for new data. Failures are reported rather than raised so held data is
    still returned.
    """
    client = entry_data.client
    try:
        await entry_data.session_manager.ensure_session()
        session = client.nest_session
        if session is None or session.is_expired():
            return None, NO_VALID_SESSION

        # get_first_data records the transport URL on the client that runs it.
        detached = NestClient(session=client.session, environment=client.environment)
        return (
            await detached.get_first_data(
                session.access_token, session.userid, request=FULL_NEST_REQUEST
            ),
            None,
        )
    # The client raises bare Exception for some Google errors.
    except Exception as err:  # noqa: BLE001
        LOGGER.debug("Diagnostics could not fetch Nest data: %s", type(err).__name__)
        return None, type(err).__name__


def _redact(data: dict[str, Any], entry_data: HomeAssistantNestProtectData) -> dict:
    """Redact by key, then scrub account identifiers embedded in other values.

    Nest keys several buckets by user id (e.g. ``user.<userid>``), so key-based
    redaction alone would leak it through object keys and references.
    """
    redacted = async_redact_data(data, TO_REDACT)
    session = entry_data.client.nest_session
    if session is None:
        return redacted
    return _scrub(redacted, [s for s in (session.userid, session.email) if s])


def _scrub(value: Any, secrets: list[str]) -> Any:
    """Replace every occurrence of the given strings, in keys and values."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, Mapping):
        return {_scrub(k, secrets): _scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(item, secrets) for item in value]
    return value
