"""Sensor entities for Narwal robot vacuum."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTime, UnitOfArea
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import WorkingStatus

_LOGGER = logging.getLogger(__name__)

STATUS_NAMES = {
    WorkingStatus.UNKNOWN: "Unknown",
    WorkingStatus.STANDBY: "Standby",
    WorkingStatus.PAUSED: "Paused",
    WorkingStatus.SLEEPING: "Sleeping",
    WorkingStatus.CLEANING: "Cleaning",
    WorkingStatus.CLEANING_ALT: "Cleaning",
    WorkingStatus.RETURNING: "Returning",
    WorkingStatus.CHARGING: "Charging",
    WorkingStatus.MOP_WASHING: "Washing Mop",
    WorkingStatus.MOP_DRYING: "Drying Mop",
    WorkingStatus.DOCKED: "Docked",
    WorkingStatus.DUST_COLLECTING: "Emptying Dustbin",
    WorkingStatus.UPGRADING: "Upgrading",
    WorkingStatus.SELF_CHECK: "Self Check",
    WorkingStatus.CHARGED: "Charged",
    WorkingStatus.ERROR: "Error",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal sensor entities."""
    coordinator = entry.runtime_data
    async_add_entities([
        NarwalBatterySensor(coordinator),
        NarwalStatusSensor(coordinator),
        NarwalElapsedTimeSensor(coordinator),
        NarwalCleanedAreaSensor(coordinator),
    ])


class NarwalBatterySensor(NarwalEntity, SensorEntity):
    """Battery level sensor."""

    _attr_translation_key = "battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.data['device_name']}_battery"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.data
        if state is None:
            return None
        return max(0.0, min(100.0, state.battery_level))


class NarwalStatusSensor(NarwalEntity, SensorEntity):
    """Working status sensor."""

    _attr_translation_key = "status"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.data['device_name']}_status"

    @property
    def native_value(self) -> str:
        state = self.coordinator.data
        if state is None:
            return "Unknown"
        if not state.device_reachable:
            return "Sleeping"
        return STATUS_NAMES.get(state.working_status, "Unknown")

    @property
    def extra_state_attributes(self) -> dict:
        state = self.coordinator.data
        if state is None:
            return {}
        return {
            "working_status_code": state.working_status.value,
            "is_cleaning": state.is_cleaning,
            "is_paused": state.is_paused,
            "is_returning": state.is_returning,
            "is_docked": state.is_docked,
        }


class NarwalElapsedTimeSensor(NarwalEntity, SensorEntity):
    """Time elapsed in the current or most recent cleaning session."""

    _attr_translation_key = "elapsed_time"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_icon = "mdi:timer-outline"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.data['device_name']}_elapsed_time"

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.data
        if state is None:
            return None
        return state.elapsed_time


class NarwalCleanedAreaSensor(NarwalEntity, SensorEntity):
    """Area covered in the current or most recent cleaning session."""

    _attr_translation_key = "cleaned_area"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "m²"
    _attr_icon = "mdi:texture-box"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.data['device_name']}_cleaned_area"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.data
        if state is None:
            return None
        return state.cleaned_area / 10000.0  # cm² to m²
