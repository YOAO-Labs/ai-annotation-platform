"""v0.23.5 · ADR-0052 §D6 · gzip transport contract for ``coco_rle`` mask objects.

Mask canonical JSON can run into the MiB range for 4K / multi-instance masks.
The gzip transport layer keeps wire / object-storage size under control while
remaining backward compatible with the legacy uncompressed ``.json`` path.

Two boundaries are enforced on decompression to close the zip-bomb vector
(`ADR-0052 §D6 <docs/adr/archive/0052-shared-raster-mask-and-image-geometry.md>`__):

- ``MAX_COMPRESSED_BYTES`` — total compressed bytes consumed.
- ``MAX_UNCOMPRESSED_BYTES`` — total decompressed bytes produced.
- ``MAX_EXPANSION_RATIO`` — ratio ceiling (``max_uncompressed / max_compressed``),
  enforced by the uncompressed cap combined with the ratio guard.

Streaming ``zlib.decompressobj`` (``wbits = MAX_WBITS | 16`` for gzip) lets us
abort the moment any cap is breached instead of materializing the full payload.
"""

from __future__ import annotations

import gzip
import zlib

# v0.23.5 · ADR-0052 §D6 · bounded decompress constants (module-level so tests
# and callers can import the same source of truth).
# NOTE: ``MAX_UNCOMPRESSED_BYTES`` mirrors ``MAX_RLE_OBJECT_BYTES`` in
# ``app.services.raster_mask_storage`` (both 4 MiB per ADR-0048 / ADR-0052).
# We duplicate the literal here rather than importing it to avoid a circular
# import (raster_mask_storage imports from this module). A unit test in
# ``tests/test_mask_gzip_transport.py`` asserts the two stay in sync.
MAX_COMPRESSED_BYTES = 8 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024
MAX_EXPANSION_RATIO = 20


def compress_mask_gzip(data: bytes) -> bytes:
    """Compress canonical bytes with gzip (level 6) for symmetric storage/tests."""
    return gzip.compress(data, compresslevel=6)


def decompress_mask_gzip(
    raw: bytes,
    *,
    max_compressed: int = MAX_COMPRESSED_BYTES,
    max_uncompressed: int = MAX_UNCOMPRESSED_BYTES,
    max_ratio: int = MAX_EXPANSION_RATIO,
) -> bytes:
    """Streamingly decompress a gzip payload while bounding every zip-bomb vector.

    ``raw`` is fed to ``zlib.decompressobj`` in chunks (so a hostile payload
    can't force us to materialize the full compressed buffer in one syscall).
    After each chunk we check:

    - the running compressed-byte count stays under ``max_compressed``;
    - the running decompressed-byte count stays under ``max_uncompressed``;
    - the (uncompressed / compressed) ratio stays under ``max_ratio`` — this
      is a sharper early-rejection than the absolute cap alone: a tiny
      compressed payload that already exceeds ``max_ratio`` is rejected even
      when it would fit the absolute cap, matching the ADR's intent.

    The ratio cap can also be expressed purely via ``max_uncompressed`` (a
    payload with ``compressed < max_uncompressed / max_ratio`` cannot reach
    ``max_uncompressed`` without violating ``max_ratio``). We enforce both
    explicitly so the rejection message names which limit was breached and so
    a future tuning change to either constant stays self-contained.

    Raises ``ValueError`` with a clear message on any overflow or decode error.
    """
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
        # max_length bounds allocation even for a malicious high-ratio stream.
        out = bytearray(decompressor.decompress(raw, max_uncompressed + 1))
    except zlib.error as exc:  # malformed / not actually gzip
        raise ValueError(f"gzip decode failed: {exc}") from exc
    if len(out) > max_uncompressed:
        raise ValueError(
            "gzip decompressed payload exceeds "
            f"MAX_UNCOMPRESSED_BYTES={max_uncompressed}"
        )
    if decompressor.unconsumed_tail:
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
