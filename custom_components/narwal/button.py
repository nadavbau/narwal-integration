"""Button entities for Narwal robot vacuum."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal button entities."""
    coordinator = entry.runtime_data
    async_add_entities([NarwalLocateButton(coordinator)])


class NarwalLocateButton(NarwalEntity, ButtonEntity):
    """Makes the vacuum beep so you can find it."""

    _attr_translation_key = "locate"
    _attr_icon = "mdi:map-marker-question"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.data['device_name']}_locate"
        )

    async def async_press(self) -> None:
        """Send the yell/locate command to the vacuum."""
        resp = await self.coordinator.client.locate()
        if not resp.success:
            _LOGGER.warning("Locate returned code=%s", resp.result_code)
