"""Map renderer for Narwal vacuum — converts raw map data to PNG bytes.

Map data format (confirmed from live robot data):
  - Field 17 of the map protobuf is compressed with zlib
  - Decompressed data is a protobuf wrapper: field 1 = packed repeated varints
  - Skip the protobuf header (0x0a + varint length), then decode varints
  - Each varint encodes: room_id = value >> 8, pixel_type = value & 0xFF
  - Value 0 = unknown/outside, 0x20 = unassigned floor, 0x28 = unassigned obstacle
  - pixel_type & 0x10 = wall/border edge (darker shade of room color)
"""

from __future__ import annotations

import io
import logging
import zlib

_LOGGER = logging.getLogger(__name__)

ROOM_COLORS: list[tuple[int, int, int]] = [
    (100, 149, 237),  # 1 - cornflower blue
    (144, 238, 144),  # 2 - light green
    (255, 182, 193),  # 3 - light pink
    (255, 218, 185),  # 4 - peach
    (221, 160, 221),  # 5 - plum
    (176, 224, 230),  # 6 - powder blue
    (255, 255, 150),  # 7 - light yellow
    (188, 143, 143),  # 8 - rosy brown
    (152, 251, 152),  # 9 - pale green
    (135, 206, 250),  # 10 - light sky blue
    (240, 128, 128),  # 11 - light coral
    (216, 191, 216),  # 12 - thistle
]

COLOR_UNKNOWN = (40, 40, 40)
COLOR_UNASSIGNED_FLOOR = (200, 200, 200)
COLOR_UNASSIGNED_OBSTACLE = (80, 80, 80)
COLOR_FALLBACK = (180, 180, 180)


def _decode_packed_varints(data: bytes) -> list[int]:
    """Decode protobuf packed repeated varint field.

    The decompressed grid starts with a protobuf header:
      byte 0: 0x0a (field 1, wire type 2 = length-delimited)
      bytes 1+: varint length
    After the header, remaining bytes are packed varint pixel values.
    """
    if len(data) < 4:
        return []

    pos = 0
    if data[0] == 0x0A:
        pos = 1
        while pos < len(data) and data[pos] & 0x80:
            pos += 1
        pos += 1

    pixels: list[int] = []
    while pos < len(data):
        val = 0
        shift = 0
        while pos < len(data):
            b = data[pos]
            pos += 1
            val |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
        pixels.append(val)
    return pixels


def _darken(color: tuple[int, int, int], amount: int = 80) -> tuple[int, int, int]:
    return (
        max(0, color[0] - amount),
        max(0, color[1] - amount),
        max(0, color[2] - amount),
    )


def render_map(
    compressed_grid: bytes,
    width: int,
    height: int,
    room_names: dict[int, str] | None = None,
    scale: int = 2,
) -> bytes:
    """Render compressed map grid data as a PNG image.

    Args:
        compressed_grid: Zlib-compressed grid data from map protobuf field 17.
        width: Map width in pixels (field 4).
        height: Map height in pixels (field 5).
        room_names: Optional mapping of room_id to display name for labels.
        scale: Upscale factor for visibility (default 2x).

    Returns:
        PNG image as bytes, or empty bytes on failure.
    """
    if not compressed_grid or width <= 0 or height <= 0:
        return b""

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        _LOGGER.error("Pillow is required for map rendering — pip install Pillow")
        return b""

    try:
        decompressed = zlib.decompress(compressed_grid)
    except zlib.error:
        try:
            decompressed = zlib.decompress(compressed_grid, 47)
        except zlib.error:
            _LOGGER.warning("Could not decompress map grid (%d bytes)", len(compressed_grid))
            return b""

    pixels = _decode_packed_varints(decompressed)
    expected = width * height

    if len(pixels) < expected:
        _LOGGER.debug("Map has %d pixels, expected %d — padding", len(pixels), expected)
        pixels.extend([0] * (expected - len(pixels)))
    elif len(pixels) > expected:
        pixels = pixels[:expected]

    img = Image.new("RGB", (width, height), COLOR_UNKNOWN)
    px = img.load()

    room_sum_x: dict[int, int] = {}
    room_sum_y: dict[int, int] = {}
    room_count: dict[int, int] = {}

    for i, val in enumerate(pixels):
        x = i % width
        y = i // width

        if val == 0:
            continue
        elif val == 0x20:
            px[x, y] = COLOR_UNASSIGNED_FLOOR
        elif val == 0x28:
            px[x, y] = COLOR_UNASSIGNED_OBSTACLE
        else:
            room_id = val >> 8
            ptype = val & 0xFF

            if 1 <= room_id <= len(ROOM_COLORS):
                base = ROOM_COLORS[room_id - 1]
            else:
                base = COLOR_FALLBACK

            if ptype & 0x10:
                px[x, y] = _darken(base)
            else:
                px[x, y] = base

            if room_names and room_id in room_names and not (ptype & 0x10):
                room_sum_x[room_id] = room_sum_x.get(room_id, 0) + x
                room_sum_y[room_id] = room_sum_y.get(room_id, 0) + y
                room_count[room_id] = room_count.get(room_id, 0) + 1

    img = img.transpose(Image.FLIP_TOP_BOTTOM)

    if room_names and room_count:
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("arial.ttf", 10)
        except (IOError, OSError):
            font = ImageFont.load_default()

        for rid, name in room_names.items():
            if not name or rid not in room_count:
                continue
            cx = room_sum_x[rid] // room_count[rid]
            cy = height - 1 - (room_sum_y[rid] // room_count[rid])
            bbox = font.getbbox(name)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            tx = cx - tw // 2
            ty = cy - th // 2
            for ox, oy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                draw.text((tx + ox, ty + oy), name, fill=(0, 0, 0), font=font)
            draw.text((tx, ty), name, fill=(255, 255, 255), font=font)

    if scale > 1:
        img = img.resize((width * scale, height * scale), Image.NEAREST)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
