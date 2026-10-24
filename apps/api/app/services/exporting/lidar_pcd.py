"""Bounded PCD record validation without decoding or changing point order."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
import re
import struct
from typing import BinaryIO, Literal, NoReturn


MAX_PCD_BYTES = 256 * 1024 * 1024
MAX_PCD_POINTS = 4_000_000
MAX_POINTMASK_INDICES = 32_000_000
MAX_HEADER_BYTES = 64 * 1024
MAX_ROW_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 128
MAX_FIELDS = 128
MAX_FIELD_COUNT = 1024
MAX_POINT_SCALARS = 1024
MAX_POINT_STRIDE = 4096
MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024
_READ_BYTES = 64 * 1024
_HISTORY_BYTES = 8192
_HISTORY_MASK = _HISTORY_BYTES - 1
_UNSIGNED = re.compile(rb"[0-9]+\Z")
_INTEGER = re.compile(rb"[+-]?[0-9]+\Z")
_FLOAT = re.compile(
    rb"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|inf(?:inity)?|nan)\Z",
    re.IGNORECASE,
)
_FIELD_NAME = re.compile(rb"[A-Za-z_][A-Za-z_0-9]*\Z")
_SCALARS = {("F", 4), ("F", 8)} | {
    (kind, size) for kind in ("I", "U") for size in (1, 2, 4)
}
_FLOAT32_MAX = struct.unpack("<f", b"\xff\xff\x7f\x7f")[0]
_HEADER_KEYS = {
    "VERSION",
    "FIELDS",
    "SIZE",
    "TYPE",
    "COUNT",
    "WIDTH",
    "HEIGHT",
    "VIEWPOINT",
    "POINTS",
    "DATA",
}


@dataclass(frozen=True)
class PcdInfo:
    point_count: int
    data_kind: Literal["ascii", "binary", "binary_compressed"]
    point_stride: int
    body_offset: int
    size_bytes: int


@dataclass(frozen=True)
class _Field:
    name: str
    kind: str
    size: int
    count: int


def _reject(code: str) -> NoReturn:
    raise ValueError(code)


def _tokens(value: bytes) -> list[bytes]:
    tokens = value.split()
    if any(len(token) > MAX_TOKEN_BYTES for token in tokens):
        _reject("pcd_token_budget_exceeded")
    return tokens


def _unsigned(token: bytes) -> int:
    if len(token) > MAX_TOKEN_BYTES or _UNSIGNED.fullmatch(token) is None:
        _reject("pcd_header_invalid")
    return int(token)


def _one_integer(header: dict[str, list[bytes]], name: str) -> int:
    tokens = header.get(name, [])
    if len(tokens) != 1:
        _reject("pcd_header_invalid")
    return _unsigned(tokens[0])


def _read_header(file: BinaryIO, size_bytes: int) -> tuple[PcdInfo, tuple[_Field, ...]]:
    header: dict[str, list[bytes]] = {}
    while True:
        line = file.readline(MAX_HEADER_BYTES + 1)
        if not isinstance(line, bytes):
            _reject("pcd_stream_invalid")
        if file.tell() > MAX_HEADER_BYTES:
            _reject("pcd_header_budget_exceeded")
        if not line:
            _reject("pcd_header_invalid")
        line = line.partition(b"#")[0].strip()
        if not line:
            continue
        parts = _tokens(line)
        try:
            key = parts[0].decode("ascii").upper()
        except UnicodeDecodeError:
            _reject("pcd_header_invalid")
        if key not in _HEADER_KEYS or key in header or len(parts) < 2:
            _reject("pcd_header_invalid")
        header[key] = parts[1:]
        if key == "DATA":
            break

    fields = header.get("FIELDS", [])
    if not fields or len(fields) > MAX_FIELDS:
        _reject("pcd_field_budget_exceeded")
    if any(_FIELD_NAME.fullmatch(name) is None for name in fields):
        _reject("pcd_header_invalid")
    names = [name.decode("ascii").lower() for name in fields]
    if len(set(names)) != len(names) or not {"x", "y", "z"}.issubset(names):
        _reject("pcd_fields_invalid")
    sizes = header.get("SIZE", [])
    kinds = header.get("TYPE", [])
    counts = header.get("COUNT", [b"1"] * len(fields))
    if not (len(fields) == len(sizes) == len(kinds) == len(counts)):
        _reject("pcd_fields_invalid")
    layout = []
    for name, size, kind, count in zip(names, sizes, kinds, counts, strict=True):
        try:
            scalar_kind = kind.decode("ascii").upper()
        except UnicodeDecodeError:
            _reject("pcd_fields_invalid")
        scalar_size, scalar_count = _unsigned(size), _unsigned(count)
        if (scalar_kind, scalar_size) not in _SCALARS or scalar_count < 1:
            _reject("pcd_fields_invalid")
        if scalar_count > MAX_FIELD_COUNT:
            _reject("pcd_field_budget_exceeded")
        if name in {"x", "y", "z"} and scalar_count != 1:
            _reject("pcd_fields_invalid")
        layout.append(_Field(name, scalar_kind, scalar_size, scalar_count))
    stride = sum(field.size * field.count for field in layout)
    if (
        stride > MAX_POINT_STRIDE
        or sum(field.count for field in layout) > MAX_POINT_SCALARS
    ):
        _reject("pcd_field_budget_exceeded")
    width, height = _one_integer(header, "WIDTH"), _one_integer(header, "HEIGHT")
    point_count = width * height
    if max(width, height, point_count) > MAX_PCD_POINTS:
        _reject("pcd_point_budget_exceeded")
    if "POINTS" in header and _one_integer(header, "POINTS") != point_count:
        _reject("pcd_point_count_mismatch")
    if "VERSION" in header and header["VERSION"] not in (
        [b".7"],
        [b"0.7"],
        [b".6"],
        [b"0.6"],
    ):
        _reject("pcd_header_invalid")
    if "VIEWPOINT" in header:
        values = header["VIEWPOINT"]
        if len(values) != 7 or any(_FLOAT.fullmatch(value) is None for value in values):
            _reject("pcd_header_invalid")
        if not all(math.isfinite(float(value)) for value in values):
            _reject("pcd_header_invalid")
    mode = header["DATA"]
    if mode not in ([b"ascii"], [b"binary"], [b"binary_compressed"]):
        _reject("pcd_data_mode_unsupported")
    data_kind = mode[0].decode("ascii")
    return PcdInfo(point_count, data_kind, stride, file.tell(), size_bytes), tuple(
        layout
    )


def _validate_ascii(file: BinaryIO, info: PcdInfo, fields: tuple[_Field, ...]) -> None:
    columns = sum(field.count for field in fields)
    rows = 0
    while line := file.readline(MAX_ROW_BYTES + 1):
        if not isinstance(line, bytes):
            _reject("pcd_stream_invalid")
        if len(line) > MAX_ROW_BYTES:
            _reject("pcd_row_budget_exceeded")
        tokens = _tokens(line)
        if not tokens:
            continue
        rows += 1
        if rows > info.point_count:
            _reject("pcd_point_count_mismatch")
        if len(tokens) != columns:
            _reject("pcd_ascii_row_invalid")
        cursor = 0
        for field in fields:
            for token in tokens[cursor : cursor + field.count]:
                if field.kind == "F":
                    if _FLOAT.fullmatch(token) is None:
                        _reject("pcd_ascii_value_invalid")
                    value = float(token)
                    explicit_special = token.lower().lstrip(b"+-") in {
                        b"nan",
                        b"inf",
                        b"infinity",
                    }
                    if not math.isfinite(value) and not explicit_special:
                        _reject("pcd_ascii_value_invalid")
                    if (
                        field.size == 4
                        and math.isfinite(value)
                        and abs(value) > _FLOAT32_MAX
                    ):
                        _reject("pcd_ascii_value_invalid")
                else:
                    if _INTEGER.fullmatch(token) is None:
                        _reject("pcd_ascii_value_invalid")
                    value = int(token)
                    bits = field.size * 8
                    lower = -(1 << (bits - 1)) if field.kind == "I" else 0
                    upper = (1 << (bits - (field.kind == "I"))) - 1
                    if not lower <= value <= upper:
                        _reject("pcd_ascii_value_invalid")
            cursor += field.count
    if rows != info.point_count:
        _reject("pcd_point_count_mismatch")


class _LzfInput:
    """Read the compressed body once with bounded buffering and exact accounting."""

    def __init__(self, file: BinaryIO, length: int):
        self.file, self.remaining = file, length
        self.buffer, self.offset = b"", 0

    def take(self, length: int) -> bytes:
        if length > self.remaining:
            _reject("pcd_lzf_invalid")
        value = bytearray()
        while length:
            if self.offset == len(self.buffer):
                self.buffer = self.file.read(min(_READ_BYTES, self.remaining))
                self.offset = 0
                if not isinstance(self.buffer, bytes) or not self.buffer:
                    _reject("pcd_lzf_invalid")
            part = min(length, len(self.buffer) - self.offset)
            value.extend(self.buffer[self.offset : self.offset + part])
            self.offset += part
            self.remaining -= part
            length -= part
        return bytes(value)

    def byte(self) -> int:
        return self.take(1)[0]


def _append_history(history: bytearray, produced: int, data: bytes) -> None:
    start = produced & _HISTORY_MASK
    first = min(len(data), _HISTORY_BYTES - start)
    history[start : start + first] = data[:first]
    history[: len(data) - first] = data[first:]


def _validate_lzf(file: BinaryIO, compressed: int, expected: int) -> None:
    source = _LzfInput(file, compressed)
    history = bytearray(_HISTORY_BYTES)
    produced = 0
    while source.remaining:
        control = source.byte()
        if control < 32:
            data = source.take(control + 1)
        else:
            length = control >> 5
            if length == 7:
                length += source.byte()
            distance = ((control & 31) << 8) + source.byte() + 1
            length += 2
            if distance > produced:
                _reject("pcd_lzf_invalid")
            # A back-reference can overlap its output. Repeat the existing
            # distance-byte pattern without retaining the decompressed body.
            seed_size = min(distance, length)
            start = (produced - distance) & _HISTORY_MASK
            first = min(seed_size, _HISTORY_BYTES - start)
            seed = bytes(history[start : start + first] + history[: seed_size - first])
            data = (seed * ((length + seed_size - 1) // seed_size))[:length]
        if produced + len(data) > expected:
            _reject("pcd_lzf_invalid")
        _append_history(history, produced, data)
        produced += len(data)
    if produced != expected:
        _reject("pcd_lzf_invalid")


def _compressed_lengths(file: BinaryIO) -> tuple[int, int]:
    value = bytearray()
    while len(value) < 8:
        part = file.read(8 - len(value))
        if not isinstance(part, bytes) or not part:
            _reject("pcd_compressed_length_mismatch")
        value.extend(part)
    return struct.unpack("<II", value)


def inspect_pcd(file: BinaryIO) -> PcdInfo:
    """Validate a borrowed verified stage, preserving its cursor even on failure.

    Record count includes non-finite XYZ values. No point matrix, file ownership,
    coordinate conversion, sampling or original-content authority is created.
    """
    if not file.seekable():
        _reject("pcd_stream_invalid")
    position = file.tell()
    try:
        file.seek(0, os.SEEK_END)
        size = file.tell()
        if size > MAX_PCD_BYTES:
            _reject("pcd_byte_budget_exceeded")
        file.seek(0)
        info, fields = _read_header(file, size)
        body_size = size - info.body_offset
        expected = info.point_count * info.point_stride
        if info.data_kind == "ascii":
            _validate_ascii(file, info, fields)
        elif info.data_kind == "binary":
            if body_size != expected:
                _reject("pcd_binary_length_mismatch")
        else:
            if expected > MAX_DECOMPRESSED_BYTES:
                _reject("pcd_decompressed_budget_exceeded")
            compressed, decompressed = _compressed_lengths(file)
            if compressed + 8 != body_size or decompressed != expected:
                _reject("pcd_compressed_length_mismatch")
            _validate_lzf(file, compressed, expected)
        return info
    finally:
        file.seek(position)
