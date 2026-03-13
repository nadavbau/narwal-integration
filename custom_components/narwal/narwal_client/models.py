"""Data models for Narwal vacuum state."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from .const import WorkingStatus


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
            if 1 in sub and isinstance(sub[1], int):
                try:
                    self.working_status = WorkingStatus(sub[1])
                except ValueError:
                    self.working_status = WorkingStatus.UNKNOWN
            self.is_paused = sub.get(2, 0) == 1 or sub.get(4, 0) == 1
            self.is_returning = sub.get(7, 0) == 1
            self.is_docked = sub.get(10, 0) == 1

        self.is_cleaning = self.working_status in (
            WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT
        )
        if self.working_status in (WorkingStatus.DOCKED, WorkingStatus.CHARGED):
            self.is_docked = True

    def update_working_status(self, payload: bytes) -> None:
        """Update from working_status protobuf."""
        fields = parse_protobuf_fields(payload)
        self.raw_working_status = fields

        if 3 in fields:
            self.elapsed_time = fields[3]
        if 13 in fields:
            self.cleaned_area = fields[13]


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
