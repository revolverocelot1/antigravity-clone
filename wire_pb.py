"""Minimal, round-trip-safe protobuf wire parser & re-encoder.

Preserves unknown fields, nested blobs, and exact byte ordering while allowing
targeted field rewrites (e.g., rootConversationId, status, title, annotations).
"""

from __future__ import annotations

from typing import Callable, List, Tuple, Union

WireValue = Union[int, bytes]
WireField = Tuple[int, int, WireValue]  # (field_number, wire_type, value)


def _encode_varint(n: int) -> bytes:
    if n < 0:
        n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _decode_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    val = 0
    shift = 0
    length = len(buf)
    while pos < length:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
        if shift > 70:
            raise ValueError("Malformed varint (>10 bytes)")
    raise ValueError("Truncated varint")


def parse_fields(buf: bytes) -> List[WireField]:
    """Decode top-level protobuf wire fields. Raises ValueError on unsupported/corrupt data."""
    fields: List[WireField] = []
    pos = 0
    length = len(buf)
    while pos < length:
        tag, pos = _decode_varint(buf, pos)
        fn = tag >> 3
        wt = tag & 0x07
        if fn == 0:
            raise ValueError(f"Invalid field number 0 at offset {pos}")
        if wt == 0:
            val, pos = _decode_varint(buf, pos)
            fields.append((fn, wt, val))
        elif wt == 1:
            if pos + 8 > length:
                raise ValueError("Truncated fixed64 field")
            fields.append((fn, wt, buf[pos : pos + 8]))
            pos += 8
        elif wt == 2:
            size, pos = _decode_varint(buf, pos)
            if size < 0 or pos + size > length:
                raise ValueError("Truncated length-delimited field")
            fields.append((fn, wt, buf[pos : pos + size]))
            pos += size
        elif wt == 5:
            if pos + 4 > length:
                raise ValueError("Truncated fixed32 field")
            fields.append((fn, wt, buf[pos : pos + 4]))
            pos += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wt} at field {fn}")
    return fields


def encode_fields(fields: List[WireField]) -> bytes:
    """Serialize a list of (field_num, wire_type, value) back to bytes."""
    out = bytearray()
    for fn, wt, val in fields:
        out.extend(_encode_varint((fn << 3) | wt))
        if wt == 0:
            assert isinstance(val, int)
            out.extend(_encode_varint(val))
        elif wt in (1, 5):
            assert isinstance(val, (bytes, bytearray))
            out.extend(val)
        elif wt == 2:
            assert isinstance(val, (bytes, bytearray))
            out.extend(_encode_varint(len(val)))
            out.extend(val)
        else:
            raise ValueError(f"Unsupported wire type {wt}")
    return bytes(out)


def rewrite_string_field(
    buf: bytes,
    field_num: int,
    new_value: str,
    *,
    only_if_matches: str | None = None,
    add_if_missing: bool = False,
) -> bytes:
    """Replace UTF-8 string field `field_num` while keeping all other fields intact."""
    fields = parse_fields(buf)
    encoded = new_value.encode("utf-8")
    target_bytes = only_if_matches.encode("utf-8") if only_if_matches is not None else None
    replaced = False
    new_fields: List[WireField] = []
    for fn, wt, val in fields:
        if fn == field_num and wt == 2:
            if target_bytes is None or val == target_bytes:
                new_fields.append((fn, wt, encoded))
                replaced = True
                continue
        new_fields.append((fn, wt, val))
    if not replaced and add_if_missing:
        new_fields.append((field_num, 2, encoded))
    return encode_fields(new_fields)


def rewrite_varint_field(
    buf: bytes,
    field_num: int,
    new_value: int,
    *,
    add_if_missing: bool = False,
) -> bytes:
    """Replace varint field `field_num` while keeping all other fields intact."""
    fields = parse_fields(buf)
    replaced = False
    new_fields: List[WireField] = []
    for fn, wt, val in fields:
        if fn == field_num and wt == 0:
            new_fields.append((fn, wt, new_value))
            replaced = True
        else:
            new_fields.append((fn, wt, val))
    if not replaced and add_if_missing:
        new_fields.append((field_num, 0, new_value))
    return encode_fields(new_fields)


def rewrite_bytes_field(
    buf: bytes,
    field_num: int,
    transform: Callable[[bytes], bytes],
) -> bytes:
    """Apply `transform` to length-delimited field `field_num` if present."""
    fields = parse_fields(buf)
    new_fields: List[WireField] = []
    for fn, wt, val in fields:
        if fn == field_num and wt == 2 and isinstance(val, bytes):
            new_fields.append((fn, wt, transform(val)))
        else:
            new_fields.append((fn, wt, val))
    return encode_fields(new_fields)


def remove_field(buf: bytes, field_num: int) -> bytes:
    """Remove all occurrences of `field_num` while keeping other fields intact."""
    fields = [(fn, wt, val) for fn, wt, val in parse_fields(buf) if fn != field_num]
    return encode_fields(fields)


def upsert_bytes_field(
    buf: bytes,
    field_num: int,
    transform: Callable[[bytes], bytes],
) -> bytes:
    """Apply `transform` to field `field_num`, inserting it with `transform(b"")` if absent."""
    fields = parse_fields(buf)
    found = False
    new_fields: List[WireField] = []
    for fn, wt, val in fields:
        if fn == field_num and wt == 2 and isinstance(val, bytes):
            new_fields.append((fn, wt, transform(val)))
            found = True
        else:
            new_fields.append((fn, wt, val))
    if not found:
        new_fields.append((field_num, 2, transform(b"")))
    return encode_fields(new_fields)


def extract_first_string(buf: bytes, field_num: int) -> str:
    """Best-effort extract UTF-8 string for length-delimited field `field_num`."""
    try:
        for fn, wt, val in parse_fields(buf):
            if fn == field_num and wt == 2 and isinstance(val, bytes):
                return val.decode("utf-8", errors="replace")
    except ValueError:
        pass
    return ""


def get_first_varint(buf: bytes, field_num: int) -> int | None:
    """Extract first varint value for `field_num`, or None if missing/invalid."""
    try:
        for fn, wt, val in parse_fields(buf):
            if fn == field_num and wt == 0 and isinstance(val, int):
                return val
    except ValueError:
        pass
    return None
