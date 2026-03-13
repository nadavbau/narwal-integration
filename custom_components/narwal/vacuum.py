"""Vacuum entity for Narwal robot vacuum."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import DOMAIN, FAN_SPEED_LIST, FAN_SPEED_MAP
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import NarwalCommandError, WorkingStatus

_LOGGER = logging.getLogger(__name__)

WORKING_STATUS_TO_ACTIVITY: dict[WorkingStatus, VacuumActivity] = {
    WorkingStatus.DOCKED: VacuumActivity.DOCKED,
    WorkingStatus.CHARGED: VacuumActivity.DOCKED,
    WorkingStatus.STANDBY: VacuumActivity.IDLE,
    WorkingStatus.CLEANING: VacuumActivity.CLEANING,
    WorkingStatus.CLEANING_ALT: VacuumActivity.CLEANING,
    WorkingStatus.ERROR: VacuumActivity.ERROR,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Narwal vacuum entity."""
    coordinator = entry.runtime_data
    async_add_entities([NarwalVacuum(coordinator)])


class NarwalVacuum(NarwalEntity, StateVacuumEntity):
    """Representation of a Narwal robot vacuum."""

    _attr_translation_key = "narwal"
    _attr_supported_features = (
        VacuumEntityFeature.STATE
        | VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.LOCATE
    )
    _attr_fan_speed_list = FAN_SPEED_LIST

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.data['device_name']}_vacuum"
        self._last_fan_speed: str | None = None

    @property
    def activity(self) -> VacuumActivity:
        state = self.coordinator.data
        if state is None:
            return VacuumActivity.IDLE

        if state.is_paused and state.is_cleaning:
            return VacuumActivity.PAUSED
        if state.is_returning:
            return VacuumActivity.RETURNING
        if state.is_cleaning:
            return VacuumActivity.CLEANING
        if state.is_docked:
            return VacuumActivity.DOCKED

        return WORKING_STATUS_TO_ACTIVITY.get(
            state.working_status, VacuumActivity.IDLE
        )

    @property
    def fan_speed(self) -> str | None:
        return self._last_fan_speed

    async def async_start(self) -> None:
        state = self.coordinator.data
        if state and state.is_paused and state.is_cleaning:
            await self.coordinator.client.resume()
        else:
            resp = await self.coordinator.client.start()
            if not resp.success:
                _LOGGER.warning("Start command did not succeed (code=%s)", resp.result_code)

    async def async_stop(self, **kwargs: Any) -> None:
        await self.coordinator.client.stop()

    async def async_pause(self) -> None:
        await self.coordinator.client.pause()

    async def async_return_to_base(self, **kwargs: Any) -> None:
        resp = await self.coordinator.client.return_to_base()
        if not resp.success:
            _LOGGER.warning("Return-to-base did not succeed (code=%s)", resp.result_code)

    async def async_locate(self, **kwargs: Any) -> None:
        await self.coordinator.client.locate()

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        from .narwal_client import FanLevel
        level = FAN_SPEED_MAP.get(fan_speed)
        if level is not None:
            await self.coordinator.client.set_fan_speed(FanLevel(level))
            self._last_fan_speed = fan_speed
            self.async_write_ha_state()
