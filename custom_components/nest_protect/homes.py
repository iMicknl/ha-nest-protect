"""Choose which Nest homes (structures) a config entry covers."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Any

import voluptuous as vol
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import CONF_HOMES
from .pynest.enums import BucketType
from .pynest.models import Bucket

DEVICE_BUCKET_TYPES = {BucketType.TOPAZ, BucketType.KRYPTONITE}
# Buckets keyed by the structure ID itself.
HOME_BUCKET_TYPES = {BucketType.STRUCTURE, BucketType.SAFETY_SUMMARY, BucketType.WHERE}

STRUCTURE_PREFIX = f"{BucketType.STRUCTURE}."
GRPC_DEVICE_PREFIX = "DEVICE_"


def account_homes(buckets: Iterable[Any]) -> dict[str, str]:
    """Return the account's homes as structure ID to name."""
    homes: dict[str, str] = {}
    for bucket in buckets:
        if bucket.object_key.startswith(STRUCTURE_PREFIX):
            structure_id = bucket.object_key.removeprefix(STRUCTURE_PREFIX)
            homes[structure_id] = bucket.value.get("name") or structure_id
    return homes


def excluded_bucket_keys(buckets: list[Bucket], selected: Collection[str]) -> set[str]:
    """Return the keys of buckets and devices in homes that are not selected."""
    # The structure's swarm also places devices without a structure_id, and
    # locks, which have no bucket here and are matched by device ID later.
    home_of: dict[str, str] = {}
    for bucket in buckets:
        if bucket.type == BucketType.STRUCTURE and isinstance(
            swarm := bucket.value.get("swarm"), list
        ):
            structure_id = bucket.object_key.removeprefix(STRUCTURE_PREFIX)
            home_of |= {key: structure_id for key in swarm if isinstance(key, str)}

    for bucket in buckets:
        if bucket.type in HOME_BUCKET_TYPES:
            home_of[bucket.object_key] = bucket.object_key.partition(".")[2]
        elif bucket.type in DEVICE_BUCKET_TYPES:
            structure_id = bucket.value.get("structure_id")
            if isinstance(structure_id, str) and structure_id:
                home_of[bucket.object_key] = structure_id.removeprefix(STRUCTURE_PREFIX)

    # Devices whose home is unknown are kept: hiding a device of a selected
    # home is worse than showing one of another home.
    excluded = {key for key, home in home_of.items() if home not in selected}
    # A Protect's online state is in a bucket keyed by the same device ID.
    excluded |= {
        f"{BucketType.WIDGET_TRACK}.{key.partition('.')[2]}"
        for key in excluded
        if key.startswith(f"{BucketType.TOPAZ}.")
    }
    return excluded


def is_excluded_resource(resource_id: str, excluded_keys: Collection[str]) -> bool:
    """Return whether a gRPC device is in the swarm of a home that isn't selected."""
    device_id = resource_id.removeprefix(GRPC_DEVICE_PREFIX).upper()
    return any(key.partition(".")[2].upper() == device_id for key in excluded_keys)


def homes_options(selected: Collection[str], homes: Collection[str]) -> dict[str, Any]:
    """Return the entry options for a selection of homes."""
    # Without a filter, homes added to the account later are picked up too.
    if set(homes) <= set(selected):
        return {}
    return {CONF_HOMES: sorted(selected)}


def homes_schema(homes: dict[str, str]) -> vol.Schema:
    """Return a form schema to pick one or more homes."""
    return vol.Schema(
        {
            vol.Required(CONF_HOMES): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=structure_id, label=name)
                        for structure_id, name in homes.items()
                    ],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        }
    )
