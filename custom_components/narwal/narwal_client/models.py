"""Data models for Narwal vacuum state."""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from .const import ROOM_NAME_CODES, WorkingStatus

_LOGGER = logging.getLogger(__name__)


@dataclass
class CommandResponse:
    """Parsed command response."""

    result_code: int = 0
    success: bool = False
    raw: bytes = b""
    data: bytes = b""

    @classmethod
    def from_payload(cls, payload: bytes) -> CommandResponse:
        """Parse response: field 1 (varint) = success, field 2 (bytes) = data payload."""
        resp = cls(raw=payload)
        fields = parse_protobuf_fields(payload)
        if 1 in fields and isinstance(fields[1], int):
            resp.result_code = fields[1]
            resp.success = resp.result_code == 1
        if 2 in fields and isinstance(fields[2], bytes):
            resp.data = fields[2]
        return resp


@dataclass
class NarwalState:
    """Current vacuum state assembled from status broadcasts."""

    working_status: WorkingStatus = WorkingStatus.UNKNOWN
    battery_level: float = 0.0
    is_cleaning: bool = False
    is_paused: bool = False
    is_returning: bool = False
    is_docked: bool = False
    elapsed_time: int = 0
    cleaned_area: int = 0

    # Raw protobuf data for fields we don't fully decode yet
    raw_base_status: dict = field(default_factory=dict)
    raw_working_status: dict = field(default_factory=dict)

    def update_base_status(self, payload: bytes) -> None:
        """Update from robot_base_status protobuf.

        The payload field layout (confirmed via captures):
          field 2 (fixed32) = battery level as IEEE 754 float32
          field 3 (sub-message) = mode/state
            sub-field 1 = working status enum
            sub-field 2 = is_paused (in some captures, sub-field 4)
            sub-field 7 = is_returning
            sub-field 10 = dock sub-state (1=docked)
          field 13 (string) = user UUID
        """
        fields = parse_protobuf_fields(payload)
        self.raw_base_status = fields
        _LOGGER.debug(
            "Parsed base status fields: %s",
            {
                k: (v.hex() if isinstance(v, bytes) else v)
                for k, v in fields.items()
            },
        )

        # Field 2 = battery as IEEE 754 float32 (little-endian fixed32)
        if 2 in fields:
            val = fields[2]
            if isinstance(val, int):
                try:
                    self.battery_level = struct.unpack('<f', struct.pack('<I', val))[0]
                except Exception:
                    self.battery_level = float(val)
            elif isinstance(val, (float, int)):
                self.battery_level = float(val)

        # Field 3 = mode/state sub-message
        if 3 in fields and isinstance(fields[3], bytes):
            sub = parse_protobuf_fields(fields[3])
            _LOGGER.debug("Base status sub-fields: %s", sub)
            if 1 in sub and isinstance(sub[1], int):
                raw_status = sub[1]
                try:
                    self.working_status = WorkingStatus(raw_status)
                except ValueError:
                    _LOGGER.warning(
                        "Unknown working_status value: %d, sub-fields: %s",
                        raw_status, sub,
                    )
                    self.working_status = WorkingStatus.UNKNOWN
            self.is_paused = sub.get(2, 0) == 1 or sub.get(4, 0) == 1
            self.is_returning = sub.get(7, 0) == 1
            self.is_docked = sub.get(10, 0) == 1

        self.is_cleaning = self.working_status in (
            WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT
        )
        if self.working_status == WorkingStatus.RETURNING:
            self.is_returning = True
        if self.working_status in (
            WorkingStatus.DOCKED, WorkingStatus.CHARGED,
            WorkingStatus.CHARGING, WorkingStatus.MOP_WASHING,
            WorkingStatus.MOP_DRYING, WorkingStatus.DUST_COLLECTING,
        ):
            self.is_docked = True
        _LOGGER.warning(
            "State update: status=%s battery=%.1f%% cleaning=%s paused=%s returning=%s docked=%s",
            self.working_status.name, self.battery_level,
            self.is_cleaning, self.is_paused, self.is_returning, self.is_docked,
        )

    rooms: list[RoomInfo] = field(default_factory=list)

    def update_rooms_from_map(self, map_data: bytes) -> None:
        """Extract room list from map protobuf (field 32 of map response data)."""
        fields = parse_protobuf_fields(map_data)
        raw_rooms = fields.get(32)
        if not isinstance(raw_rooms, bytes):
            return
        self.rooms = _parse_room_entries(raw_rooms)
        _LOGGER.debug("Parsed %d rooms from map data", len(self.rooms))

    def update_working_status(self, payload: bytes) -> None:
        """Update from working_status protobuf."""
        fields = parse_protobuf_fields(payload)
        self.raw_working_status = fields

        if 3 in fields:
            self.elapsed_time = fields[3]
        if 13 in fields:
            self.cleaned_area = fields[13]


@dataclass
class RoomInfo:
    """A room discovered from the vacuum's map."""

    room_id: int
    name_code: int
    name: str

    @property
    def display_name(self) -> str:
        base = self.name or ROOM_NAME_CODES.get(self.name_code, f"Room {self.room_id}")
        return f"{base} ({self.room_id})"


def _parse_room_entries(data: bytes) -> list[RoomInfo]:
    """Parse repeated room sub-messages from map field 32."""
    rooms: list[RoomInfo] = []
    idx = 0
    while idx < len(data):
        tag = data[idx]
        wire_type = tag & 0x07
        if wire_type != 2:
            break
        idx += 1
        length = 0
        shift = 0
        while idx < len(data):
            b = data[idx]
            length |= (b & 0x7F) << shift
            shift += 7
            idx += 1
            if b & 0x80 == 0:
                break
        if idx + length > len(data):
            break
        room_bytes = data[idx : idx + length]
        idx += length

        rf = parse_protobuf_fields(room_bytes)
        room_id = rf.get(1)
        name_code = rf.get(2, 0)
        custom_name = rf.get(5, "")
        if not isinstance(room_id, int):
            continue
        name = custom_name if isinstance(custom_name, str) and custom_name else ""
        rooms.append(RoomInfo(room_id=room_id, name_code=name_code, name=name))
    return rooms


def parse_protobuf_fields(data: bytes) -> dict:
    """Minimal protobuf field parser. Returns {field_num: value}.

    Handles varint (wire type 0), 64-bit (1), length-delimited (2),
    and 32-bit (5). For length-delimited fields, returns raw bytes
    if they don't look like a string.
    """
    fields: dict = {}
    idx = 0
    while idx < len(data):
        if idx >= len(data):
            break
        tag_byte = data[idx]
        wire_type = tag_byte & 0x07
        field_num = tag_byte >> 3

        # Handle multi-byte field numbers
        if tag_byte & 0x80:
            tag_val = 0
            shift = 0
            while idx < len(data):
                b = data[idx]
                tag_val |= (b & 0x7F) << shift
                shift += 7
                idx += 1
                if b & 0x80 == 0:
                    break
            wire_type = tag_val & 0x07
            field_num = tag_val >> 3
        else:
            idx += 1

        if wire_type == 0:  # varint
            val = 0
            shift = 0
            while idx < len(data):
                b = data[idx]
                val |= (b & 0x7F) << shift
                shift += 7
                idx += 1
                if b & 0x80 == 0:
                    break
            fields[field_num] = val

        elif wire_type == 1:  # 64-bit fixed
            if idx + 8 <= len(data):
                fields[field_num] = int.from_bytes(data[idx:idx+8], 'little')
                idx += 8

        elif wire_type == 2:  # length-delimited
            length = 0
            shift = 0
            while idx < len(data):
                b = data[idx]
                length |= (b & 0x7F) << shift
                shift += 7
                idx += 1
                if b & 0x80 == 0:
                    break
            if idx + length <= len(data):
                raw = data[idx:idx+length]
                try:
                    text = raw.decode('utf-8')
                    if text.isprintable():
                        fields[field_num] = text
                    else:
                        fields[field_num] = raw
                except (UnicodeDecodeError, ValueError):
                    fields[field_num] = raw
                idx += length
            else:
                break

        elif wire_type == 5:  # 32-bit fixed
            if idx + 4 <= len(data):
                fields[field_num] = int.from_bytes(data[idx:idx+4], 'little')
                idx += 4

        else:
            break  # unknown wire type

    return fields
