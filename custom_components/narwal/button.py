"""Button entities for Narwal robot vacuum."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity, entity_supported
from .narwal_client import NarwalClient


@dataclass(frozen=True, kw_only=True)
class NarwalButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[NarwalClient], Coroutine[Any, Any, Any]]


BUTTONS: tuple[NarwalButtonDescription, ...] = (
    NarwalButtonDescription(key="locate", translation_key="locate",
                            icon="mdi:map-marker-question", press_fn=lambda c: c.locate()),
    NarwalButtonDescription(key="wake", translation_key="wake",
                            icon="mdi:power", press_fn=lambda c: c.notify_active()),
    NarwalButtonDescription(key="wash_mop", translation_key="wash_mop",
                            icon="mdi:washing-machine", press_fn=lambda c: c.wash_mop()),
    NarwalButtonDescription(key="dry_mop", translation_key="dry_mop",
                            icon="mdi:fan", press_fn=lambda c: c.dry_mop()),
    NarwalButtonDescription(key="wash_and_dry_mop", translation_key="wash_and_dry_mop",
                            icon="mdi:autorenew", press_fn=lambda c: c.wash_and_dry_mop()),
    NarwalButtonDescription(key="empty_dustbin", translation_key="empty_dustbin",
                            icon="mdi:delete-empty", press_fn=lambda c: c.empty_dustbin()),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        NarwalButton(coordinator, d) for d in BUTTONS if entity_supported(coordinator, d.key)
    )


class NarwalButton(NarwalEntity, ButtonEntity):
    """A one-shot robot action."""

    entity_description: NarwalButtonDescription

    def __init__(self, coordinator: NarwalCoordinator, description: NarwalButtonDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        await self._run(self.entity_description.press_fn(self.coordinator.client))
        await self.coordinator.async_request_refresh()
