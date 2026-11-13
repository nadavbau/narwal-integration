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


COLOR_ROBOT = (255, 255, 255)
COLOR_ROBOT_OUTLINE = (20, 120, 255)
COLOR_STATION = (255, 196, 0)


def render_snapshot(
    snapshot,
    room_names: dict[int, str] | None = None,
    robot_pose=None,
    active_room_id: int | None = None,
    scale: int = 3,
) -> bytes:
    """Render a MapSnapshot with dock and robot markers, rotated like the app."""
    markers = []
    if snapshot.station:
        markers.append(("station", snapshot.to_pixel(snapshot.station), snapshot.station.theta))
    pose = robot_pose or snapshot.robot
    if pose:
        markers.append(("robot", snapshot.to_pixel(pose), pose.theta))
    return render_map(
        snapshot.grid, snapshot.width, snapshot.height, room_names,
        scale=scale, markers=markers, rotate=snapshot.rotate_angle,
        highlight_room=active_room_id,
    )


def _draw_markers_at(img, markers, scale: int) -> None:
    """Draw (kind, x, y, heading) markers given in final image coordinates."""
    import math

    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    for kind, x, y, theta in markers:
        if kind == "station":
            r = 3.5 * scale
            draw.rectangle([x - r, y - r, x + r, y + r], fill=COLOR_STATION, outline=(0, 0, 0))
        else:
            r = 4 * scale
            draw.ellipse([x - r, y - r, x + r, y + r], fill=COLOR_ROBOT, outline=COLOR_ROBOT_OUTLINE, width=max(1, scale))
            hx = x + math.cos(theta) * r
            hy = y - math.sin(theta) * r
            draw.line([x, y, hx, hy], fill=COLOR_ROBOT_OUTLINE, width=max(1, scale))


def render_map(
    compressed_grid: bytes,
    width: int,
    height: int,
    room_names: dict[int, str] | None = None,
    scale: int = 2,
    markers: list | None = None,
    rotate: int = 0,
    highlight_room: int | None = None,
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

    if highlight_room is not None:
        for i, val in enumerate(pixels):
            if val > 0x28 and (val >> 8) == highlight_room and not (val & 0x10):
                x, y = i % width, i // width
                r, g, b = px[x, y]
                px[x, y] = (min(255, r + 50), min(255, g + 50), min(255, b + 50))

    img = img.transpose(Image.FLIP_TOP_BOTTOM)
    scale = max(1, scale)
    if scale > 1:
        img = img.resize((width * scale, height * scale), Image.NEAREST)

    # Points to annotate, in scaled image coordinates (after the flip).
    labels: list[tuple[str, float, float]] = []
    if room_names and room_count:
        for rid, name in room_names.items():
            if name and rid in room_count:
                cx = (room_sum_x[rid] / room_count[rid] + 0.5) * scale
                cy = (height - 1 - room_sum_y[rid] / room_count[rid] + 0.5) * scale
                labels.append((name, cx, cy))

    # Match the app's orientation. PIL rotates counter-clockwise for positive
    # angles; rotate the annotation points the same way.
    rotate = rotate % 360
    w_img, h_img = img.size
    if rotate in (90, 180, 270):
        img = img.rotate(rotate, expand=True)

    # Margin so a dock/robot sitting on the map edge isn't clipped.
    margin = 8 * scale
    padded = Image.new("RGB", (img.size[0] + 2 * margin, img.size[1] + 2 * margin), COLOR_UNKNOWN)
    padded.paste(img, (margin, margin))
    img = padded

    def _rot(x: float, y: float) -> tuple[float, float]:
        if rotate == 90:
            x, y = y, w_img - x
        elif rotate == 180:
            x, y = w_img - x, h_img - y
        elif rotate == 270:
            x, y = h_img - y, x
        return x + margin, y + margin

    if markers:
        import math

        rotated = []
        for kind, (gx, gy), theta in markers:
            x = (gx + 0.5) * scale
            y = (height - 1 - gy + 0.5) * scale
            rx, ry = _rot(x, y)
            rotated.append((kind, rx, ry, theta + math.radians(rotate)))
        _draw_markers_at(img, rotated, scale)

    if labels:
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.load_default(size=5 * scale)  # Pillow >= 10.1
        except TypeError:
            font = ImageFont.load_default()
        for name, x, y in labels:
            tx, ty = _rot(x, y)
            bbox = draw.textbbox((0, 0), name, font=font)
            tx -= (bbox[2] - bbox[0]) / 2
            ty -= (bbox[3] - bbox[1]) / 2
            draw.text((tx, ty), name, fill=(255, 255, 255), font=font,
                      stroke_width=max(1, scale // 2), stroke_fill=(0, 0, 0))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
