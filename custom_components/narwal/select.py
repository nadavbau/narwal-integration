"""Select entities for Narwal robot vacuum."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import CLEAN_MODE_LIST, CLEAN_MODE_MAP
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import CleanMode

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal select entities."""
    coordinator = entry.runtime_data
    async_add_entities([NarwalCleanModeSelect(coordinator)])


class NarwalCleanModeSelect(NarwalEntity, SelectEntity):
    """Select entity for choosing the cleaning mode."""

    _attr_translation_key = "clean_mode"
    _attr_icon = "mdi:spray-bottle"
    _attr_options = CLEAN_MODE_LIST

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.data['device_name']}_clean_mode"
        )
        self._attr_current_option = CLEAN_MODE_LIST[0]

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        val = CLEAN_MODE_MAP.get(option)
        if val is not None:
            self.coordinator.selected_clean_mode = CleanMode(val)
        self.async_write_ha_state()
