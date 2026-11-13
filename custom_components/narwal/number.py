"""Number entities for Narwal robot vacuum."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([NarwalVolume(coordinator), NarwalCleanPasses(coordinator)])


class NarwalVolume(NarwalEntity, NumberEntity):
    """Voice volume."""

    _attr_translation_key = "volume"
    _attr_icon = "mdi:volume-high"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "volume")

    @property
    def available(self) -> bool:
        return super().available and "volumePercentage" in self.state_data.config

    @property
    def native_value(self) -> float | None:
        value = self.state_data.config.get("volumePercentage")
        return float(value) if isinstance(value, int) else None

    async def async_set_native_value(self, value: float) -> None:
        await self._run(self.coordinator.client.set_config(volumePercentage=int(value)))
        self.async_write_ha_state()


class NarwalCleanPasses(NarwalEntity, RestoreNumber):
    """How many passes each room gets when HA starts a clean (kept in HA)."""

    _attr_translation_key = "clean_passes"
    _attr_icon = "mdi:repeat"
    _attr_native_min_value = 1
    _attr_native_max_value = 3
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "clean_passes")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last and last.native_value:
            self.coordinator.clean_passes = int(last.native_value)

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float:
        return float(self.coordinator.clean_passes)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.clean_passes = int(value)
        self.async_write_ha_state()
