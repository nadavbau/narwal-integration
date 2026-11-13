"""Binary sensor entities for Narwal robot vacuum."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import NarwalState


@dataclass(frozen=True, kw_only=True)
class NarwalBinaryDescription(BinarySensorEntityDescription):
    is_on_fn: Callable[[NarwalState], bool | None]


BINARY_SENSORS: tuple[NarwalBinaryDescription, ...] = (
    NarwalBinaryDescription(
        key="charging",
        translation_key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        is_on_fn=lambda s: s.is_charging if s.base else None,
    ),
    NarwalBinaryDescription(
        key="docked",
        translation_key="docked",
        icon="mdi:home-import-outline",
        is_on_fn=lambda s: s.on_station if s.base else None,
    ),
    NarwalBinaryDescription(
        key="problem",
        translation_key="problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda s: bool(s.error_codes) if s.base else None,
    ),
    NarwalBinaryDescription(
        key="maintenance_due",
        translation_key="maintenance_due",
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:wrench-clock",
        is_on_fn=lambda s: bool(s.consumables_due.get("maintain") or s.consumables_due.get("replace")),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(NarwalBinarySensor(coordinator, d) for d in BINARY_SENSORS)


class NarwalBinarySensor(NarwalEntity, BinarySensorEntity):
    entity_description: NarwalBinaryDescription

    def __init__(self, coordinator: NarwalCoordinator, description: NarwalBinaryDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.is_on_fn(self.state_data)
