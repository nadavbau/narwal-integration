"""Schema-driven protobuf encode/decode for Narwal MQTT payloads.

The schema (field numbers, types, enums) lives in ``schema.py``. Messages are
plain dicts keyed by the field names used in the official app, e.g.::

    encode("SetFanLevel_Request", {"level": "FAN_LEVEL_STRONG",
                                    "operationType": "OPERATION_TYPE_SET_VALUE"})
    decode("GetDeviceBaseStatus_Response", payload)

Enum values decode to their label strings; unknown enum numbers stay ints.
Unknown fields decode under ``"#<number>"`` so nothing is silently lost.
"""

from __future__ import annotations

import struct
from typing import Any

from .schema import ENUMS, MESSAGES

_VARINT_TYPES = frozenset(
    {"int32", "int64", "uint32", "uint64", "sint32", "sint64", "bool", "enum"}
)
_ENUM_BY_LABEL: dict[str, dict[str, int]] = {
    name: {label: num for num, label in values.items()}
    for name, values in ENUMS.items()
}
_FIELD_BY_NAME: dict[str, dict[str, tuple[int, str, str | None, bool]]] = {
    msg: {f[0]: (num, f[1], f[2], f[3]) for num, f in fields.items()}
    for msg, fields in MESSAGES.items()
}


class ProtoError(ValueError):
    """Raised when a payload cannot be encoded or decoded."""


# --------------------------------------------------------------------------
# Wire helpers
# --------------------------------------------------------------------------


def encode_varint(value: int) -> bytes:
    """Encode an unsigned (or two's complement 64-bit) varint."""
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read a varint at ``pos``; return (value, new_pos)."""
    result = shift = 0
    while True:
        if pos >= len(data):
            raise ProtoError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, pos


def iter_fields(data: bytes):
    """Yield (field_number, wire_type, raw_value) for each field in ``data``."""
    pos = 0
    end = len(data)
    while pos < end:
        tag, pos = read_varint(data, pos)
        num, wire = tag >> 3, tag & 7
        if num == 0:
            raise ProtoError("field number 0")
        if wire == 0:
            value, pos = read_varint(data, pos)
        elif wire == 1:
            value, pos = data[pos : pos + 8], pos + 8
        elif wire == 2:
            length, pos = read_varint(data, pos)
            if pos + length > end:
                raise ProtoError("truncated length-delimited field")
            value, pos = data[pos : pos + length], pos + length
        elif wire == 5:
            value, pos = data[pos : pos + 4], pos + 4
        else:
            raise ProtoError(f"unsupported wire type {wire}")
        if pos > end:
            raise ProtoError("truncated fixed field")
        yield num, wire, value


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------


def _decode_scalar(ftype: str, ref: str | None, wire: int, raw: Any) -> Any:
    if ftype == "message":
        return decode(ref, raw) if ref else raw
    if ftype == "enum":
        return ENUMS.get(ref or "", {}).get(raw, raw)
    if ftype == "bool":
        return bool(raw)
    if ftype in ("sint32", "sint64"):
        return (raw >> 1) ^ -(raw & 1)
    if ftype in ("int32", "int64") and raw >= 1 << 63:
        return raw - (1 << 64)
    if ftype == "float":
        return struct.unpack("<f", raw)[0]
    if ftype == "double":
        return struct.unpack("<d", raw)[0]
    if ftype in ("fixed32", "sfixed32"):
        return struct.unpack("<I" if ftype == "fixed32" else "<i", raw)[0]
    if ftype in ("fixed64", "sfixed64"):
        return struct.unpack("<Q" if ftype == "fixed64" else "<q", raw)[0]
    if ftype == "string":
        return raw.decode("utf-8", errors="replace")
    return raw


def decode(message: str, data: bytes) -> dict[str, Any]:
    """Decode ``data`` as ``message``. Repeated fields become lists."""
    fields = MESSAGES.get(message, {})
    out: dict[str, Any] = {}
    for num, wire, raw in iter_fields(data):
        spec = fields.get(num)
        if spec is None:
            out[f"#{num}"] = raw
            continue
        name, ftype, ref, repeated = spec
        if wire == 2 and ftype in _VARINT_TYPES:
            # Packed repeated scalars.
            values = []
            pos = 0
            while pos < len(raw):
                val, pos = read_varint(raw, pos)
                values.append(_decode_scalar(ftype, ref, 0, val))
        else:
            values = [_decode_scalar(ftype, ref, wire, raw)]
        if repeated:
            out.setdefault(name, []).extend(values)
        else:
            out[name] = values[-1]
    return out


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------


def _encode_value(ftype: str, ref: str | None, value: Any) -> tuple[int, bytes]:
    """Return (wire_type, encoded_value_without_tag)."""
    if ftype == "message":
        if isinstance(value, (bytes, bytearray)):
            body = bytes(value)
        else:
            body = encode(ref or "", value)
        return 2, encode_varint(len(body)) + body
    if ftype == "enum":
        if isinstance(value, str):
            try:
                value = _ENUM_BY_LABEL[ref or ""][value]
            except KeyError as err:
                raise ProtoError(f"unknown {ref} value {value!r}") from err
        return 0, encode_varint(int(value))
    if ftype == "bool":
        return 0, encode_varint(1 if value else 0)
    if ftype in ("sint32", "sint64"):
        value = int(value)
        return 0, encode_varint((value << 1) ^ (value >> 63))
    if ftype in ("int32", "int64", "uint32", "uint64"):
        return 0, encode_varint(int(value))
    if ftype == "float":
        return 5, struct.pack("<f", float(value))
    if ftype == "double":
        return 1, struct.pack("<d", float(value))
    if ftype in ("fixed32", "sfixed32"):
        return 5, struct.pack("<I" if ftype == "fixed32" else "<i", int(value))
    if ftype in ("fixed64", "sfixed64"):
        return 1, struct.pack("<Q" if ftype == "fixed64" else "<q", int(value))
    if ftype == "string":
        body = str(value).encode()
        return 2, encode_varint(len(body)) + body
    if ftype == "bytes":
        body = bytes(value)
        return 2, encode_varint(len(body)) + body
    raise ProtoError(f"cannot encode type {ftype}")


def encode(message: str, values: dict[str, Any]) -> bytes:
    """Encode a dict as ``message``. Fields are written in field-number order.

    ``None`` values are skipped. Unlike proto3 defaults, explicit ``False`` and
    ``0`` ARE written: the robot treats an absent field as "leave unchanged",
    so a setting can only be turned off by sending it explicitly.
    """
    fields = _FIELD_BY_NAME.get(message)
    if fields is None:
        raise ProtoError(f"unknown message {message}")
    parts: list[tuple[int, bytes]] = []
    for name, value in values.items():
        if value is None:
            continue
        try:
            num, ftype, ref, repeated = fields[name]
        except KeyError as err:
            raise ProtoError(f"{message} has no field {name!r}") from err
        items = value if repeated and isinstance(value, (list, tuple)) else [value]
        for item in items:
            wire, body = _encode_value(ftype, ref, item)
            parts.append((num, encode_varint((num << 3) | wire) + body))
    parts.sort(key=lambda p: p[0])
    return b"".join(body for _, body in parts)


def enum_value(enum: str, label: str) -> int:
    """Return the numeric value of an enum label."""
    return _ENUM_BY_LABEL[enum][label]


def enum_labels(enum: str) -> dict[int, str]:
    """Return {value: label} for an enum."""
    return dict(ENUMS.get(enum, {}))
