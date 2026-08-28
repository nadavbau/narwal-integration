"""Button entities for Narwal robot vacuum."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client.models import CommandResponse

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal button entities."""
    coordinator = entry.runtime_data
    async_add_entities([
        NarwalLocateButton(coordinator),
        NarwalWakeButton(coordinator),
        NarwalWashMopButton(coordinator),
        NarwalDryMopButton(coordinator),
        NarwalEmptyDustbinButton(coordinator),
    ])


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


class NarwalWakeButton(NarwalEntity, ButtonEntity):
    """Wake the vacuum from sleep by sending an active_robot notification."""

    _attr_translation_key = "wake"
    _attr_icon = "mdi:power"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.data['device_name']}_wake"
        )

    async def async_press(self) -> None:
        """Send active_robot_publish to wake the vacuum."""
        await self.coordinator.client.notify_active()
        _LOGGER.info("Sent wake (active_robot) notification")


class NarwalDockActionButton(NarwalEntity, ButtonEntity):
    """Base for dock (base station) actions; only available while docked."""

    _action: str = ""  # unique_id suffix / log label

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.data['device_name']}_{self._action}"
        )

    @property
    def available(self) -> bool:
        """Dock actions only make sense while the robot is on the dock."""
        state = self.coordinator.data
        return (
            super().available
            and state is not None
            and state.device_reachable
            and state.is_docked
        )

    async def _run(self) -> CommandResponse:
        """Send this button's command to the vacuum."""
        raise NotImplementedError

    async def async_press(self) -> None:
        """Send the dock action and warn if the robot rejects it."""
        resp = await self._run()
        if not resp.success:
            _LOGGER.warning("%s returned code=%s", self._action, resp.result_code)
        else:
            _LOGGER.debug("%s sent", self._action)


class NarwalWashMopButton(NarwalDockActionButton):
    """Wash the mop pads at the dock."""

    _attr_translation_key = "wash_mop"
    _attr_icon = "mdi:water"
    _action = "wash_mop"

    async def _run(self) -> CommandResponse:
        return await self.coordinator.client.wash_mop()


class NarwalDryMopButton(NarwalDockActionButton):
    """Dry the mop pads at the dock."""

    _attr_translation_key = "dry_mop"
    _attr_icon = "mdi:weather-windy"
    _action = "dry_mop"

    async def _run(self) -> CommandResponse:
        return await self.coordinator.client.dry_mop()


class NarwalEmptyDustbinButton(NarwalDockActionButton):
    """Empty the robot's dustbin into the dock."""

    _attr_translation_key = "empty_dustbin"
    _attr_icon = "mdi:delete-empty"
    _action = "empty_dustbin"

    async def _run(self) -> CommandResponse:
        return await self.coordinator.client.empty_dustbin()
