"""Data models for Narwal vacuum state."""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from .const import ROOM_SUB_TYPE_NAMES, WorkingStatus

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
    device_reachable: bool = False

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
        sub: dict = {}
        prev_status = self.working_status
        if 3 in fields and isinstance(fields[3], bytes):
            sub = parse_protobuf_fields(fields[3])
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

        # Derive boolean flags from working_status (always, not just when
        # field 3 is present) so they stay in sync even if field 3 parsing
        # fails due to protobuf format variations.
        self.is_paused = self.working_status == WorkingStatus.PAUSED
        self.is_cleaning = self.working_status in (
            WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT
        )
        self.is_returning = self.working_status == WorkingStatus.RETURNING
        self.is_docked = self.working_status in (
            WorkingStatus.DOCKED, WorkingStatus.CHARGED,
            WorkingStatus.CHARGING, WorkingStatus.MOP_WASHING,
            WorkingStatus.MOP_DRYING, WorkingStatus.DUST_COLLECTING,
        )
        # Log state transitions at WARNING so we can diagnose enum
        # mismapping without asking the user to change log levels.
        log = _LOGGER.warning if self.working_status != prev_status else _LOGGER.debug
        log(
            "State update: status=%s battery=%.1f%% sub_fields=%s",
            self.working_status.name, self.battery_level, sub,
        )

    rooms: list[RoomInfo] = field(default_factory=list)

    def update_rooms_from_map(self, map_data: bytes) -> None:
        """Extract room list from map protobuf field 12 (repeated room entries).

        Field 12 contains user-visible rooms with proper sub_type classification
        and optional user-assigned names.  It appears as a repeated protobuf field
        so we must use parse_protobuf_repeated to collect all occurrences.
        """
        self.rooms = _parse_rooms_from_field12(map_data)
        _LOGGER.debug("Parsed %d rooms from map data (field 12)", len(self.rooms))

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
    """A room discovered from the vacuum's map.

    Parsed from map response field 12 (repeated). Each entry contains:
      field 1: room_id
      field 2: room_sub_type (ROOM_SUB_TYPE enum)
      field 3: user-assigned name (UTF-8, empty if not renamed by user)
      field 4: category (1=room, 2=utility/small space)
      field 8: instance_index (1-based, for numbering duplicates)
    """

    room_id: int
    room_sub_type: int = 0
    name: str = ""
    category: int = 0
    instance_index: int = 0

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        base = ROOM_SUB_TYPE_NAMES.get(self.room_sub_type, f"Room {self.room_id}")
        if self.instance_index > 1:
            return f"{base} {self.instance_index}"
        return base


def _parse_rooms_from_field12(map_data: bytes) -> list[RoomInfo]:
    """Extract rooms from repeated field 12 entries in the map protobuf."""
    all_fields = parse_protobuf_repeated(map_data)
    entries = all_fields.get(12, [])
    rooms: list[RoomInfo] = []
    for entry in entries:
        if not isinstance(entry, bytes):
            continue
        rf = parse_protobuf_fields(entry)
        room_id = rf.get(1)
        if not isinstance(room_id, int):
            continue
        name_raw = rf.get(3, "")
        if isinstance(name_raw, bytes):
            try:
                name = name_raw.decode("utf-8")
            except UnicodeDecodeError:
                name = ""
        elif isinstance(name_raw, str):
            name = name_raw
        else:
            name = ""
        rooms.append(RoomInfo(
            room_id=room_id,
            room_sub_type=rf.get(2, 0) if isinstance(rf.get(2), int) else 0,
            name=name,
            category=rf.get(4, 0) if isinstance(rf.get(4), int) else 0,
            instance_index=rf.get(8, 0) if isinstance(rf.get(8), int) else 0,
        ))
    return rooms


def parse_protobuf_repeated(data: bytes) -> dict[int, list]:
    """Parse protobuf collecting ALL occurrences of each field as a list.

    Standard parse_protobuf_fields keeps only the last value per field number,
    which silently drops repeated fields.  This variant returns
    {field_num: [value1, value2, ...]} so repeated entries are preserved.
    """
    fields: dict[int, list] = {}
    idx = 0
    while idx < len(data):
        tag_byte = data[idx]
        wire_type = tag_byte & 0x07
        field_num = tag_byte >> 3

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

        if wire_type == 0:
            val = 0
            shift = 0
            while idx < len(data):
                b = data[idx]
                val |= (b & 0x7F) << shift
                shift += 7
                idx += 1
                if b & 0x80 == 0:
                    break
            fields.setdefault(field_num, []).append(val)
        elif wire_type == 1:
            if idx + 8 <= len(data):
                fields.setdefault(field_num, []).append(
                    int.from_bytes(data[idx : idx + 8], "little")
                )
                idx += 8
        elif wire_type == 2:
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
                fields.setdefault(field_num, []).append(data[idx : idx + length])
                idx += length
            else:
                break
        elif wire_type == 5:
            if idx + 4 <= len(data):
                fields.setdefault(field_num, []).append(
                    int.from_bytes(data[idx : idx + 4], "little")
                )
                idx += 4
        else:
            break
    return fields


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
