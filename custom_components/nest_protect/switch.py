"""Switch platform for Nest Protect."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NestProtectConfigEntry
from .const import LOGGER
from .entity import NestHomeEntity, NestUpdatableEntity, async_run_entity_command
from .pynest.client import NestClient
from .pynest.exceptions import NestLockAuthException
from .pynest.grpc_client import GrpcClient
from .pynest.models import Bucket
from .session import NestSessionManager

PARALLEL_UPDATES = 1


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

AWAY_SWITCH_DESCRIPTION = SwitchEntityDescription(
    key="away",
    translation_key="away",
    icon="mdi:home-export-outline",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NestProtectConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Set up the Nest Protect sensors from a config entry."""

    data = entry.runtime_data
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

    entities.extend(
        NestHomeAwaySwitch(
            structure,
            AWAY_SWITCH_DESCRIPTION,
            data.structure_names.get(structure.object_key.removeprefix("structure.")),
            data.client,
            grpc_client=data.grpc_client,
            session_manager=data.session_manager,
        )
        for structure in data.structures.values()
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


class NestHomeAwaySwitch(NestHomeEntity, SwitchEntity):
    """Away mode of a Nest home.

    The state comes from the home's REST structure bucket, like the rest of this
    integration. Nest only accepts the change itself over the gRPC gateway.
    """

    def __init__(
        self,
        bucket: Bucket,
        description: SwitchEntityDescription,
        home_name: str | None,
        client: NestClient,
        *,
        grpc_client: GrpcClient,
        session_manager: NestSessionManager,
    ) -> None:
        """Initialize the switch."""
        super().__init__(bucket, description, home_name, client)
        self._grpc_client = grpc_client
        self._session_manager = session_manager

    @property
    def is_on(self) -> bool | None:
        """Return True if the home is set to Away."""
        return self.bucket.value.get("away")

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set the home to Away."""
        await self._async_set_away(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Set the home to Home."""
        await self._async_set_away(False)

    async def _async_set_away(self, away: bool) -> None:
        failure = f"Failed to update {self.entity_id}"
        resource_id = self._grpc_client.structure_resource_id(self.bucket.object_key)
        if resource_id is None:
            # Learned from the first observe stream, moments after setup.
            raise HomeAssistantError(
                f"{failure}: Nest hasn't reported this home over gRPC yet"
            )

        await async_run_entity_command(
            self,
            self._session_manager,
            lambda: self._grpc_client.send_structure_mode_command(resource_id, away),
            NestLockAuthException,
            failure,
        )
