"""Camera entity for Narwal robot vacuum map."""

from __future__ import annotations

import io
import logging
import zlib
from datetime import timedelta

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client.models import parse_protobuf_fields

_LOGGER = logging.getLogger(__name__)

MAP_REFRESH_INTERVAL = timedelta(seconds=120)


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

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Fetch the map from the vacuum and return as PNG."""
        if not self.coordinator.client.connected:
            return self._last_image

        try:
            resp = await self.coordinator.client.get_map()
        except Exception:
            _LOGGER.debug("Map fetch failed", exc_info=True)
            return self._last_image

        if not resp.raw:
            return self._last_image

        _LOGGER.info("Map response: %d raw bytes, success=%s", len(resp.raw), resp.success)

        fields = parse_protobuf_fields(resp.raw)
        _LOGGER.info(
            "Map protobuf fields: %s",
            {
                k: (
                    f"bytes({len(v)})"
                    if isinstance(v, bytes)
                    else f"str({len(v)})"
                    if isinstance(v, str)
                    else v
                )
                for k, v in fields.items()
            },
        )

        image = self._try_render_map(fields)
        if image:
            self._last_image = image
        return self._last_image

    def _try_render_map(self, fields: dict) -> bytes | None:
        """Attempt to extract or render a map image from the protobuf fields.

        Tries multiple strategies since the exact format is being discovered.
        """
        for field_num, val in fields.items():
            if not isinstance(val, bytes) or len(val) < 100:
                continue

            # Check for raw PNG/JPEG signatures
            if val[:8] == b'\x89PNG\r\n\x1a\n':
                _LOGGER.info("Found PNG image in field %d", field_num)
                return val
            if val[:2] in (b'\xff\xd8',):
                _LOGGER.info("Found JPEG image in field %d", field_num)
                return val

            # Try zlib decompression
            try:
                decompressed = zlib.decompress(val)
                _LOGGER.info(
                    "Decompressed field %d: %d -> %d bytes",
                    field_num,
                    len(val),
                    len(decompressed),
                )
                if decompressed[:8] == b'\x89PNG\r\n\x1a\n':
                    return decompressed
                # Might be raw pixel grid -- try rendering
                return self._render_grid(decompressed, fields)
            except zlib.error:
                pass

            # Try nested protobuf for image data
            try:
                nested = parse_protobuf_fields(val)
                for nk, nv in nested.items():
                    if isinstance(nv, bytes) and len(nv) > 100:
                        if nv[:8] == b'\x89PNG\r\n\x1a\n':
                            return nv
                        try:
                            dec = zlib.decompress(nv)
                            if dec[:8] == b'\x89PNG\r\n\x1a\n':
                                return dec
                        except zlib.error:
                            pass
            except Exception:
                pass

        return None

    def _render_grid(self, data: bytes, fields: dict) -> bytes | None:
        """Render raw grid data as a simple PNG (grayscale)."""
        try:
            from PIL import Image

            width = 0
            height = 0
            for k, v in fields.items():
                if isinstance(v, int) and 100 < v < 2000:
                    if width == 0:
                        width = v
                    elif height == 0:
                        height = v
                        break

            if width == 0 or height == 0:
                side = int(len(data) ** 0.5)
                if side * side == len(data):
                    width = height = side
                else:
                    return None

            if width * height > len(data):
                return None

            img = Image.frombytes("L", (width, height), data[: width * height])
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except ImportError:
            _LOGGER.debug("Pillow not installed, cannot render grid map")
            return None
        except Exception:
            _LOGGER.debug("Grid render failed", exc_info=True)
            return None
