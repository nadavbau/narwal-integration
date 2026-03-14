"""Camera entity for Narwal robot vacuum map."""

from __future__ import annotations

import logging
import time
import zlib

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client.map_renderer import render_map
from .narwal_client.models import parse_protobuf_fields

_LOGGER = logging.getLogger(__name__)

MAP_CACHE_SECONDS = 120


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal camera entity for the vacuum map."""
    coordinator = entry.runtime_data
    async_add_entities([NarwalMapCamera(coordinator)])


class NarwalMapCamera(NarwalEntity, Camera):
    """Displays the cleaning map from the Narwal vacuum."""

    _attr_translation_key = "map"
    _attr_icon = "mdi:floor-plan"
    _attr_frame_interval = 30

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        NarwalEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._attr_unique_id = (
            f"{coordinator.config_entry.data['device_name']}_map"
        )
        self._last_image: bytes | None = None
        self._last_fetch: float = 0.0

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Fetch the map from the vacuum and return as PNG."""
        if not self.coordinator.client.connected:
            return self._last_image

        now = time.monotonic()
        if now - self._last_fetch < MAP_CACHE_SECONDS and self._last_image:
            return self._last_image

        try:
            resp = await self.coordinator.client.get_map()
            self._last_fetch = now
        except Exception:
            _LOGGER.debug("Map fetch failed", exc_info=True)
            return self._last_image

        if not resp.data:
            return self._last_image

        try:
            fields = parse_protobuf_fields(resp.data)
            grid_width = fields.get(4, 0)
            grid_height = fields.get(5, 0)
            compressed_grid = fields.get(17, b"")

            if not isinstance(compressed_grid, bytes) or grid_width <= 0 or grid_height <= 0:
                _LOGGER.debug(
                    "Map missing required fields: w=%s h=%s grid=%d bytes",
                    grid_width, grid_height,
                    len(compressed_grid) if isinstance(compressed_grid, bytes) else 0,
                )
                return self._last_image

            room_names = {
                r.room_id: r.display_name
                for r in self.coordinator.client.state.rooms
            }

            image = await self.hass.async_add_executor_job(
                render_map, compressed_grid, grid_width, grid_height, room_names,
            )

            if image:
                self._last_image = image
        except Exception:
            _LOGGER.debug("Map render failed", exc_info=True)

        return self._last_image
