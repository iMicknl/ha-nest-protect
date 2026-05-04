"""Select platform for Nest Protect."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.helpers.entity import EntityCategory

from . import HomeAssistantNestProtectData
from .const import DOMAIN, LOGGER
from .entity import NestUpdatableEntity


@dataclass
class NestProtectSelectDescription(SelectEntityDescription):
    """Class to describe an Nest Protect sensor."""


BRIGHTNESS_TO_PRESET: dict[str, str] = {1: "low", 2: "medium", 3: "high"}

PRESET_TO_BRIGHTNESS = {v: k for k, v in BRIGHTNESS_TO_PRESET.items()}


SENSOR_DESCRIPTIONS: list[SelectEntityDescription] = [
    NestProtectSelectDescription(
        key="night_light_brightness",
        translation_key="night_light_brightness",
        name="Brightness",
        icon="mdi:lightbulb-on",
        options=[*PRESET_TO_BRIGHTNESS],
        entity_category=EntityCategory.CONFIG,
    ),
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Set up the Nest Protect sensors from a config entry."""

    data: HomeAssistantNestProtectData = hass.data[DOMAIN][entry.entry_id]
    entities: list[NestProtectSelect] = []

    supported_keys: dict[str, NestProtectSelectDescription] = {
        description.key: description for description in SENSOR_DESCRIPTIONS
    }

    for device in data.devices.values():
        for key in device.value:
            if description := supported_keys.get(key):
                entities.append(
                    NestProtectSelect(
                        device,
                        description,
                        data.areas,
                        data.client,
                        data.session_manager,
                    )
                )

    async_add_devices(entities)


class NestProtectSelect(NestUpdatableEntity, SelectEntity):
    """Representation of a Nest Protect Select."""

    entity_description: NestProtectSelectDescription

    @property
    def current_option(self) -> str:
        """Return the selected entity option to represent the entity state."""
        state = self.bucket.value.get(self.entity_description.key)
        return BRIGHTNESS_TO_PRESET.get(state)

    @property
    def options(self) -> list[str]:
        """Return a set of selectable options."""
        return self.entity_description.options

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        select = PRESET_TO_BRIGHTNESS.get(option)

        objects = [
            {
                "object_key": self.bucket.object_key,
                "op": "MERGE",
                "value": {
                    self.entity_description.key: select,
                },
            }
        ]

        result = await self._async_update_objects(objects)
        LOGGER.debug(result)
