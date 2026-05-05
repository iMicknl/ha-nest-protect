"""Switch platform for Nest Protect."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.helpers.entity import EntityCategory

from . import HomeAssistantNestProtectData
from .const import DOMAIN, LOGGER
from .entity import NestUpdatableEntity


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
    entities: list[NestProtectSwitch] = []

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
