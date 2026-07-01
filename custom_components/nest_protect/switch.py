"""Switch platform for Nest Protect."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory

from . import HomeAssistantNestProtectData
from .const import ATTRIBUTION, DOMAIN, LOGGER
from .entity import NestUpdatableEntity
from .pynest.models import Bucket


@dataclass
class NestProtectSwitchDescriptionMixin:
    """Define an entity description mixin for select entities."""

    # options: list[str]
    # select_option: Callable[[str, Callable[..., Awaitable[None]]], Awaitable[None]]


@dataclass
class NestProtectSwitchDescription(
    SwitchEntityDescription, NestProtectSwitchDescriptionMixin
):
    """Class to describe an Nest Protect sensor."""


BRIGHTNESS_TO_PRESET: dict[str, str] = {1: "low", 2: "medium", 3: "high"}

PRESET_TO_BRIGHTNESS = {v: k for k, v in BRIGHTNESS_TO_PRESET.items()}


SWITCH_DESCRIPTIONS: list[SwitchEntityDescription] = [
    NestProtectSwitchDescription(
        key="night_light_enable",
        translation_key="night_light_enable",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:weather-night",
    ),
    NestProtectSwitchDescription(
        key="ntp_green_led_enable",
        translation_key="ntp_green_led_enable",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:led-off",
    ),
    NestProtectSwitchDescription(
        key="heads_up_enable",
        translation_key="heads_up_enable",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:exclamation-thick",
    ),
    NestProtectSwitchDescription(
        key="steam_detection_enable",
        translation_key="steam_detection_enable",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:pot-steam",
    ),
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Set up the Nest Protect sensors from a config entry."""

    data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = []

    supported_keys: dict[str, NestProtectSwitchDescription] = {
        description.key: description for description in SWITCH_DESCRIPTIONS
    }

    for device in data.devices.values():
        for key in device.value:
            if description := supported_keys.get(key):
                entities.append(
                    NestProtectSwitch(
                        device,
                        description,
                        data.areas,
                        data.client,
                        data.session_manager,
                    )
                )

    for structure in data.structures.values():
        entities.append(
            NestStructureHomeAwaySwitch(
                structure,
                data.client,
                data.session_manager,
            )
        )

    async_add_devices(entities)


class NestProtectSwitch(NestUpdatableEntity, SwitchEntity):
    """Representation of a Nest Protect Switch."""

    entity_description: NestProtectSwitchDescription

    @property
    def is_on(self) -> bool | None:
        """Return True if entity is on."""
        return self.bucket.value.get(self.entity_description.key)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        objects = [
            {
                "object_key": self.bucket.object_key,
                "op": "MERGE",
                "value": {
                    self.entity_description.key: True,
                },
            }
        ]

        result = await self._async_update_objects(objects)
        LOGGER.debug(result)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        objects = [
            {
                "object_key": self.bucket.object_key,
                "op": "MERGE",
                "value": {
                    self.entity_description.key: False,
                },
            }
        ]

        result = await self._async_update_objects(objects)
        LOGGER.debug(result)


class NestStructureHomeAwaySwitch(SwitchEntity):
    """Representation of a Nest structure Home/Away switch."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_attribution = ATTRIBUTION
    _attr_translation_key = "home_away"

    def __init__(self, bucket: Bucket, client, session_manager) -> None:
        """Initialize the structure Home/Away switch."""
        self.bucket = bucket
        self.client = client
        self.session_manager = session_manager
        self._attr_unique_id = f"{bucket.object_key}-home_away"
        self._attr_device_info = self._generate_device_info()

    @property
    def is_on(self) -> bool | None:
        """Return True if the structure is home."""
        away = self.bucket.value.get("away")
        if away is None:
            return None
        return not away

    def _generate_device_info(self) -> DeviceInfo:
        structure_id = self.bucket.object_key.split(".", 1)[1]
        name = self.bucket.value.get("name") or "Nest Home"
        return DeviceInfo(
            identifiers={(DOMAIN, structure_id)},
            name=name,
            manufacturer="Google",
            configuration_url=f"https://home.nest.com/home/{structure_id}",
        )

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to register update signal handler."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.bucket.object_key, self.update_callback
            )
        )

    @callback
    def update_callback(self, bucket: Bucket):
        """Update the entity state."""
        self.bucket = bucket
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set the structure to Home."""
        await self._async_set_home(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Set the structure to Away."""
        await self._async_set_home(False)

    async def _async_set_home(self, home: bool) -> None:
        await self.session_manager.ensure_session()

        if self.bucket.value.get("new_structure_id") and self.bucket.value.get("user_id"):
            await self.client.send_structure_mode_command(
                self.client.nest_session.access_token,
                f"STRUCTURE_{self.bucket.value['new_structure_id']}",
                self.bucket.value["user_id"],
                home,
            )
        else:
            await self.client.update_objects(
                self.client.nest_session.access_token,
                self.client.nest_session.userid,
                self.client.transport_url,
                [
                    {
                        "object_key": self.bucket.object_key,
                        "op": "MERGE",
                        "value": {
                            "away": not home,
                            "away_timestamp": int(time.time()),
                            "away_setter": 0,
                        },
                    }
                ],
            )

        self.bucket.value["away"] = not home
        self.async_write_ha_state()
