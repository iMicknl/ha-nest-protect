"""Switch platform for Nest Protect."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.helpers.entity import EntityCategory

from . import HomeAssistantNestProtectData
from .const import DOMAIN, LOGGER
from .entity import NestDescriptiveEntity


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
        name="Pathlight",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:weather-night",
    ),
    NestProtectSwitchDescription(
        key="ntp_green_led_enable",
        name="Nightly Promise",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:led-off",
    ),
    NestProtectSwitchDescription(
        key="heads_up_enable",
        name="Heads-Up",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:exclamation-thick",
    ),
    NestProtectSwitchDescription(
        key="steam_detection_enable",
        name="Steam Check",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:pot-steam",
    ),
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Set up the Nest Protect sensors from a config entry."""

    data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    entities: list[NestProtectSwitch] = []

    SUPPORTED_KEYS: dict[str, NestProtectSwitchDescription] = {
        description.key: description for description in SWITCH_DESCRIPTIONS
    }

    for device in data.devices.values():
        for key in device.value:
            if description := SUPPORTED_KEYS.get(key):
                entities.append(
                    NestProtectSwitch(device, description, data.areas, data.client)
                )

    async_add_devices(entities)


class NestProtectSwitch(NestDescriptiveEntity, SwitchEntity):
    """Representation of a Nest Protect Switch."""

    entity_description: NestProtectSwitchDescription

    @property
    def is_on(self) -> bool | None:
        """Return True if entity is on."""
        state = self.bucket.value.get(self.entity_description.key)

        return state

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

        if not self.client.nest_session or self.client.nest_session.is_expired():
            if not self.client.auth or self.client.auth.is_expired():
                await self.client.get_access_token()

            await self.client.authenticate(self.client.auth.access_token)

        result = await self.client.update_objects(
            self.client.nest_session.access_token,
            self.client.nest_session.userid,
            self.client.transport_url,
            objects,
        )

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

        if not self.client.nest_session or self.client.nest_session.is_expired():
            if not self.client.auth or self.client.auth.is_expired():
                await self.client.get_access_token()

            await self.client.authenticate(self.client.auth.access_token)

        result = await self.client.update_objects(
            self.client.nest_session.access_token,
            self.client.nest_session.userid,
            self.client.transport_url,
            objects,
        )

        LOGGER.debug(result)
