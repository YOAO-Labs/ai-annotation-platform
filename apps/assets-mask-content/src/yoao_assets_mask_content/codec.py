"""Canonical COCO RLE bytes, references and bounded gzip transport."""

from __future__ import annotations

import gzip
import hashlib
import json
import zlib
from collections.abc import Mapping
from typing import Any

from .contract import (
    MAX_COMPRESSED_BYTES,
    MAX_EXPANSION_RATIO,
    MAX_MASK_DIMENSION,
    MAX_MASK_PIXELS,
    MAX_MASK_RUNS,
    MAX_RLE_OBJECT_BYTES,
    MASK_PREFIX,
    MaskReference,
    RleContent,
)

COCO_RLE_REF_ENCODING = "coco_rle_ref"
COCO_RLE_GZIP_ENCODING = "coco_rle_gzip"
RLE_STORAGE_IDENTITY = "identity"
RLE_STORAGE_GZIP = "gzip"


def validate_coco_rle(
    rle: Mapping[str, Any] | RleContent,
) -> tuple[int, int, list[int]]:
    """Validate the stable structural RLE envelope and return normalized values."""
    if isinstance(rle, RleContent):
        # Revalidate typed values too: callers can construct a Pydantic model
        # without validation, and these helpers are also a storage boundary.
        rle = {
            "encoding": rle.encoding,
            "size": rle.size,
            "counts": rle.counts,
        }
    if not isinstance(rle, Mapping):
        raise ValueError("COCO RLE must be an object")
    if rle.get("encoding") != "coco_rle":
        raise ValueError("encoding must be 'coco_rle'")
    size = rle.get("size")
    if not isinstance(size, (list, tuple)) or len(size) != 2:
        raise ValueError("size must be [height, width]")
    height, width = size
    if type(height) is not int or height <= 0:
        raise ValueError("height must be a positive integer")
    if type(width) is not int or width <= 0:
        raise ValueError("width must be a positive integer")
    if height > MAX_MASK_DIMENSION or width > MAX_MASK_DIMENSION:
        raise ValueError(f"mask dimensions must be <= {MAX_MASK_DIMENSION}")
    pixels = height * width
    if pixels > MAX_MASK_PIXELS:
        raise ValueError(f"mask pixels must be <= {MAX_MASK_PIXELS}")
    raw_counts = rle.get("counts")
    if not isinstance(raw_counts, (list, tuple)) or not raw_counts:
        raise ValueError("counts must be a non-empty integer array")
    if len(raw_counts) > MAX_MASK_RUNS:
        raise ValueError(f"mask runs must be <= {MAX_MASK_RUNS}")
    counts: list[int] = []
    total = 0
    for index, value in enumerate(raw_counts):
        if type(value) is not int or value < 0:
            raise ValueError(f"counts[{index}] must be a non-negative integer")
        counts.append(value)
        total += value
        if total > pixels:
            raise ValueError("sum(counts) exceeds height * width")
    if total != pixels:
        raise ValueError("sum(counts) must equal height * width")
    return height, width, counts


def _rle_document(rle: Mapping[str, Any] | RleContent) -> dict[str, Any]:
    height, width, counts = validate_coco_rle(rle)
    return {"encoding": "coco_rle", "size": [height, width], "counts": counts}


def canonical_rle_bytes(rle: Mapping[str, Any] | RleContent) -> bytes:
    """Return the exact compact UTF-8 bytes used by existing RLE object keys."""
    data = json.dumps(
        _rle_document(rle), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(data) > MAX_RLE_OBJECT_BYTES:
        raise ValueError("mask RLE canonical JSON must be <= 4 MiB")
    return data


def compress_mask_gzip(data: bytes) -> bytes:
    if not isinstance(data, bytes):
        raise ValueError("mask RLE payload must be bytes")
    return gzip.compress(data, compresslevel=6)


def decompress_mask_gzip(
    raw: bytes,
    *,
    max_compressed: int = MAX_COMPRESSED_BYTES,
    max_uncompressed: int = MAX_RLE_OBJECT_BYTES,
    max_ratio: int = MAX_EXPANSION_RATIO,
) -> bytes:
    """Stream-decompress gzip with explicit byte and expansion bounds."""
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise ValueError("gzip payload must be bytes-like")
    if max_compressed <= 0 or max_uncompressed <= 0 or max_ratio <= 0:
        raise ValueError("decompress limits must be positive")
    compressed_seen = len(raw)
    if compressed_seen > max_compressed:
        raise ValueError(
            f"gzip compressed payload exceeds MAX_COMPRESSED_BYTES={max_compressed}"
        )
    if compressed_seen == 0:
        raise ValueError("gzip payload is empty")
    decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
    try:
        out = bytearray(decompressor.decompress(raw, max_uncompressed + 1))
    except zlib.error as exc:
        raise ValueError(f"gzip decode failed: {exc}") from exc
    if len(out) > max_uncompressed or decompressor.unconsumed_tail:
        raise ValueError(
            "gzip decompressed payload exceeds "
            f"MAX_UNCOMPRESSED_BYTES={max_uncompressed}"
        )
    if not decompressor.eof:
        raise ValueError("gzip payload is truncated")
    if decompressor.unused_data:
        raise ValueError("gzip payload contains concatenated or trailing data")
    if len(out) > compressed_seen * max_ratio:
        raise ValueError(
            f"gzip expansion ratio exceeds MAX_EXPANSION_RATIO={max_ratio}"
        )
    return bytes(out)


def canonical_rle_bytes_gzip(
    rle: Mapping[str, Any] | RleContent,
) -> tuple[bytes, bytes]:
    data = canonical_rle_bytes(rle)
    gzip_bytes = compress_mask_gzip(data)
    if len(gzip_bytes) > MAX_COMPRESSED_BYTES:
        raise ValueError("mask RLE gzip payload exceeds MAX_COMPRESSED_BYTES")
    return data, gzip_bytes


def rle_object_key(digest: str) -> str:
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise ValueError("mask digest must be lowercase SHA-256")
    return f"{MASK_PREFIX}/{digest[:2]}/{digest[2:4]}/{digest}.json"


def rle_gzip_object_key(digest: str) -> str:
    return f"{MASK_PREFIX}/{digest[:2]}/{digest[2:4]}/{digest}.json.gz"


def build_rle_reference(rle: Mapping[str, Any] | RleContent) -> MaskReference:
    data = canonical_rle_bytes(rle)
    digest = hashlib.sha256(data).hexdigest()
    height, width, counts = validate_coco_rle(rle)
    return MaskReference(
        encoding=COCO_RLE_REF_ENCODING,
        size=(height, width),
        object_key=rle_object_key(digest),
        sha256=digest,
        runs=len(counts),
        bytes=len(data),
    )


def build_rle_gzip_reference(
    rle: Mapping[str, Any] | RleContent,
) -> MaskReference:
    data, gzip_bytes = canonical_rle_bytes_gzip(rle)
    if len(data) > len(gzip_bytes) * MAX_EXPANSION_RATIO:
        raise ValueError("mask RLE exceeds the bounded gzip expansion ratio")
    digest = hashlib.sha256(data).hexdigest()
    height, width, counts = validate_coco_rle(rle)
    return MaskReference(
        encoding=COCO_RLE_REF_ENCODING,
        storage_encoding=RLE_STORAGE_GZIP,
        size=(height, width),
        object_key=rle_gzip_object_key(digest),
        sha256=digest,
        runs=len(counts),
        bytes=len(data),
    )


def validate_reference_for_rle(
    reference: MaskReference | Mapping[str, Any],
    rle: Mapping[str, Any] | RleContent,
) -> None:
    ref = (
        reference
        if isinstance(reference, MaskReference)
        else MaskReference.model_validate(reference)
    )
    data = canonical_rle_bytes(rle)
    digest = hashlib.sha256(data).hexdigest()
    height, width, counts = validate_coco_rle(rle)
    key = ref.object_key
    expected_key = (
        rle_gzip_object_key(digest)
        if key.endswith(".json.gz")
        else rle_object_key(digest)
    )
    if (
        key != expected_key
        or ref.sha256 != digest
        or list(ref.size) != [height, width]
        or ref.runs != len(counts)
        or ref.bytes != len(data)
    ):
        raise ValueError("AAP mask object metadata mismatch")


def decode_reference(
    reference: MaskReference | Mapping[str, Any], raw: bytes
) -> RleContent:
    """Validate stored bytes and return the canonical portable COCO RLE value."""
    ref = (
        reference
        if isinstance(reference, MaskReference)
        else MaskReference.model_validate(reference)
    )
    if not isinstance(raw, bytes):
        raise ValueError("stored Mask object must be bytes")
    gzip_key = ref.object_key.endswith(".json.gz")
    if gzip_key:
        data = decompress_mask_gzip(raw)
    else:
        if len(raw) > MAX_RLE_OBJECT_BYTES:
            raise ValueError("stored mask RLE exceeds 4 MiB")
        data = raw
    if len(data) > MAX_RLE_OBJECT_BYTES:
        raise ValueError("stored mask RLE exceeds 4 MiB")
    digest = hashlib.sha256(data).hexdigest()
    expected_key = rle_gzip_object_key(digest) if gzip_key else rle_object_key(digest)
    if digest != ref.sha256 or ref.object_key != expected_key:
        raise ValueError("stored mask RLE digest mismatch")
    if len(data) != ref.bytes:
        raise ValueError("stored mask RLE byte count mismatch")
    try:
        parsed = json.loads(
            data.decode("utf-8"),
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("non-finite JSON")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("stored mask RLE JSON is invalid") from exc
    height, width, counts = validate_coco_rle(parsed)
    if [height, width] != list(ref.size):
        raise ValueError("stored mask RLE size mismatch")
    if len(counts) != ref.runs:
        raise ValueError("stored mask RLE run count mismatch")
    if canonical_rle_bytes(parsed) != data:
        raise ValueError("stored mask RLE JSON is not canonical")
    return RleContent(encoding="coco_rle", size=(height, width), counts=tuple(counts))
