"""Camera entity showing the Narwal vacuum map."""

from __future__ import annotations

import logging
import time

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client.map_renderer import render_snapshot

_LOGGER = logging.getLogger(__name__)

# If the map hasn't loaded yet, ask for it at most this often.
MAP_RETRY_SECONDS = 300


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([NarwalMapCamera(entry.runtime_data)])


class NarwalMapCamera(NarwalEntity, Camera):
    """Renders the coordinator's map with live robot position.

    The coordinator owns map fetching (on setup, after each task, and every
    30 min); this entity only renders, so dashboards can't flood the robot
    with map requests.
    """

    _attr_translation_key = "map"
    _attr_icon = "mdi:floor-plan"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        NarwalEntity.__init__(self, coordinator, "map")
        Camera.__init__(self)
        self.content_type = "image/png"
        self._image: bytes | None = None
        self._rendered_key: tuple | None = None
        self._last_retry = 0.0

    @property
    def available(self) -> bool:
        # The last map is still useful while the robot sleeps.
        return self._image is not None or super().available

    @property
    def frame_interval(self) -> float:
        return 2.0 if self.state_data.is_cleaning else 30.0

    @callback
    def _handle_coordinator_update(self) -> None:
        # Bump the access token so the frontend re-fetches when the map/pose changes.
        if self._render_key() != self._rendered_key:
            self.async_update_token()
        super()._handle_coordinator_update()

    def _render_key(self) -> tuple:
        state = self.state_data
        pose = state.robot_pose
        return (
            state.map.fetched_at if state.map else None,
            round(pose.x, 1) if pose else None,
            round(pose.y, 1) if pose else None,
            state.current_room_id if state.is_cleaning else None,
            tuple((r.room_id, r.display_name) for r in state.rooms),
        )

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        state = self.state_data
        if state.map is None:
            now = time.monotonic()
            if self.coordinator.client.connected and now - self._last_retry > MAP_RETRY_SECONDS:
                self._last_retry = now
                await self.coordinator._refresh_map()
            if state.map is None:
                return self._image

        key = self._render_key()
        if key == self._rendered_key and self._image:
            return self._image
        try:
            image = await self.hass.async_add_executor_job(
                render_snapshot,
                state.map,
                {r.room_id: r.display_name for r in state.rooms},
                state.robot_pose,
                state.current_room_id if state.is_cleaning else None,
            )
        except Exception:  # noqa: BLE001 - never break the dashboard
            _LOGGER.debug("Map render failed", exc_info=True)
            return self._image
        if image:
            self._image = image
            self._rendered_key = key
        return self._image
