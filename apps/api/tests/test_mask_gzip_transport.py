"""v0.23.5 · WS-D · D1 · gzip transport contract for coco_rle mask objects.

Covers ``app.utils.raster_mask_gzip`` (bounded decompress) and the new
``store_coco_rle_gzip`` / ``load_coco_rle`` async paths in
``app.services.raster_mask_storage``. Backward compatibility with legacy
uncompressed ``.json`` references is asserted alongside the gzip path.

ADR-0052 §D6 freezes the contract: SHA-256 is over the **uncompressed**
canonical bytes, ``bytes`` records the uncompressed length, and any decompress
overflow raises ``ValueError`` immediately.
"""

from __future__ import annotations

import hashlib
import inspect
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from app.schemas._jsonb_types import CocoRleMaskRef
from app.services.raster_object_storage import RasterObjectStorageError
from app.services.raster_mask_storage import (
    MAX_RLE_OBJECT_BYTES,
    canonical_rle_bytes,
    canonical_rle_bytes_gzip,
    load_coco_rle,
    store_coco_rle,
    store_coco_rle_gzip,
)
from app.utils.raster_mask_gzip import (
    MAX_COMPRESSED_BYTES,
    MAX_EXPANSION_RATIO,
    MAX_UNCOMPRESSED_BYTES,
    compress_mask_gzip,
    decompress_mask_gzip,
)
from tests.test_raster_mask_storage import memory_mask_storage as _storage
from tests.test_raster_mask_storage import mask_owner_for, ready_mask_owner

RLE = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 2, 1]}


def test_constants_match_adr_0052_d6():
    """ADR-0052 §D6 freezes 8 MiB compressed / 4 MiB uncompressed / ratio 20."""
    assert MAX_COMPRESSED_BYTES == 8 * 1024 * 1024
    assert MAX_UNCOMPRESSED_BYTES == 4 * 1024 * 1024
    assert MAX_EXPANSION_RATIO == 20
    # MAX_UNCOMPRESSED_BYTES must mirror MAX_RLE_OBJECT_BYTES (duplicated in
    # raster_mask_gzip to avoid a circular import — guard against drift).
    assert MAX_UNCOMPRESSED_BYTES == MAX_RLE_OBJECT_BYTES


def test_gzip_reference_schema_accepts_new_and_legacy_contracts():
    from app.services.raster_mask_storage import build_rle_gzip_reference

    reference = build_rle_gzip_reference(RLE)
    assert CocoRleMaskRef.model_validate(reference).storage_encoding == "gzip"
    legacy = {**reference, "encoding": "coco_rle_gzip"}
    legacy.pop("storage_encoding")
    assert CocoRleMaskRef.model_validate(legacy).encoding == "coco_rle_gzip"


def test_decompress_mask_gzip_roundtrip():
    """compress known RLE JSON → decompress → equal."""
    canonical = canonical_rle_bytes(RLE)
    gz = compress_mask_gzip(canonical)
    out = decompress_mask_gzip(gz)
    assert out == canonical
    # Real gzip framing (not raw zlib) — first two bytes are the gzip magic.
    assert gz[:2] == b"\x1f\x8b"


def test_decompress_mask_gzip_rejects_over_uncompressed():
    """payload that decompresses > 4 MiB → ValueError naming the limit."""
    big = b"x" * (MAX_UNCOMPRESSED_BYTES + 1)
    gz = compress_mask_gzip(big)
    with pytest.raises(ValueError, match="MAX_UNCOMPRESSED_BYTES"):
        decompress_mask_gzip(gz)


def test_decompress_mask_gzip_rejects_over_compressed():
    """input whose compressed size exceeds the cap → ValueError naming the cap.

    The default ``MAX_COMPRESSED_BYTES`` (8 MiB) is larger than
    ``MAX_UNCOMPRESSED_BYTES`` (4 MiB), so for valid gzip the uncompressed cap
    fires first. This test tightens ``max_compressed`` below the payload size
    to exercise the compressed-byte counter directly — the protection that
    bounds how many bytes we ever read from a hostile/lying input stream.
    """
    data = b"x" * (2 * 1024 * 1024)  # 2 MiB → compresses well under 1 MiB
    gz = compress_mask_gzip(data)
    # Tighten max_compressed below len(gz) so the compressed counter trips.
    with pytest.raises(ValueError, match="MAX_COMPRESSED_BYTES"):
        decompress_mask_gzip(gz, max_compressed=len(gz) - 1)


def test_decompress_mask_gzip_rejects_bomb_high_ratio():
    """small compressed → large uncompressed exceeding ratio → ValueError.

    A 1 MiB all-zero payload compresses to ~1 KiB (ratio ≈ 1000 ≫ 20). Even
    though it fits both absolute caps, the ratio guard rejects it as a zip-bomb
    signature.
    """
    bomb = b"\x00" * (1024 * 1024)
    bomb_gz = compress_mask_gzip(bomb)
    assert len(bomb_gz) < MAX_COMPRESSED_BYTES  # compressed side is tiny
    with pytest.raises(ValueError, match="MAX_EXPANSION_RATIO"):
        decompress_mask_gzip(bomb_gz)


def test_decompress_mask_gzip_rejects_malformed_payload():
    """not actually gzip → ValueError (not zlib.error / 500)."""
    with pytest.raises(ValueError, match="gzip decode failed"):
        decompress_mask_gzip(b"this is not gzip at all")


def test_decompress_mask_gzip_rejects_truncated_concatenated_and_trailing():
    payload = compress_mask_gzip(canonical_rle_bytes(RLE))
    with pytest.raises(ValueError, match="truncated"):
        decompress_mask_gzip(payload[:-2])
    with pytest.raises(ValueError, match="concatenated or trailing"):
        decompress_mask_gzip(payload + compress_mask_gzip(b"second member"))
    with pytest.raises(ValueError, match="concatenated or trailing"):
        decompress_mask_gzip(payload + b"junk")


def test_compress_mask_gzip_is_symmetric_and_idempotent():
    """compress + decompress is identity; same input → same output bytes."""
    data = canonical_rle_bytes(RLE)
    a = compress_mask_gzip(data)
    b = compress_mask_gzip(data)
    assert a == b
    assert decompress_mask_gzip(a) == data


async def test_store_coco_rle_gzip_content_addressed(db_session):
    """gzip path stores .json.gz, sha256 over uncompressed canonical bytes,
    and the reference schema matches the uncompressed form except for
    object_key suffix + encoding marker."""
    storage = _storage()
    reference = await store_coco_rle_gzip(RLE, storage=storage, db=db_session)

    assert reference["encoding"] == "coco_rle_ref"
    assert reference["storage_encoding"] == "gzip"
    assert reference["object_key"].endswith(f"{reference['sha256']}.json.gz")
    # sha256 is over the uncompressed canonical bytes (ADR-0052 §D6).
    canonical = canonical_rle_bytes(RLE)
    assert reference["sha256"] == hashlib.sha256(canonical).hexdigest()
    # bytes = uncompressed canonical length (NOT the gzipped length).
    assert reference["bytes"] == len(canonical)
    assert reference["runs"] == 4
    assert reference["size"] == [2, 3]

    # put_object was called with the gzipped body (smaller than canonical).
    put_kwargs = storage.client.put_object.call_args.kwargs
    assert put_kwargs["ContentType"] == "application/gzip"
    owner = await mask_owner_for(db_session, reference)
    assert put_kwargs["Key"] == owner.storage_ref["object_key"]
    assert put_kwargs["Key"] == f"raster-masks/objects/{owner.id}.json.gz"
    assert put_kwargs["Key"] != reference["object_key"]
    stored_body = put_kwargs["Body"]
    # The stored body is gzip bytes (magic header) and decompresses to canonical.
    assert stored_body[:2] == b"\x1f\x8b"
    assert decompress_mask_gzip(stored_body) == canonical


async def test_load_coco_rle_gzip_roundtrip(db_session):
    """store gzip → load → original counts; all digest/byte/run checks pass."""
    storage = _storage()
    reference = await store_coco_rle_gzip(RLE, storage=storage, db=db_session)
    body = storage.client.put_object.call_args.kwargs["Body"]
    stream = BytesIO(body)
    stream.close = MagicMock()
    storage.open_body = lambda _raw: stream

    payload = await load_coco_rle(reference, storage=storage, db=db_session)
    assert payload["counts"] == [1, 2, 2, 1]
    assert payload["size"] == [2, 3]
    assert payload["encoding"] == "coco_rle"
    stream.close.assert_called_once()


async def test_load_coco_rle_backward_compat_json(db_session):
    """The unchanged portable JSON DTO resolves its independent UUID owner."""
    storage = _storage()
    reference = await store_coco_rle(RLE, storage=storage, db=db_session)
    assert reference["object_key"].endswith(".json")
    body = storage.client.put_object.call_args.kwargs["Body"]
    stream = BytesIO(body)
    stream.close = MagicMock()
    storage.open_body = lambda _raw: stream

    payload = await load_coco_rle(reference, storage=storage, db=db_session)
    assert payload["counts"] == [1, 2, 2, 1]


async def test_gzip_writer_falls_back_when_reader_ratio_would_reject(db_session):
    highly_compressible = {
        "encoding": "coco_rle",
        "size": [1, 4000],
        "counts": [0] * 4000 + [4000],
    }
    storage = _storage()
    reference = await store_coco_rle_gzip(
        highly_compressible, storage=storage, db=db_session
    )
    assert reference["encoding"] == "coco_rle_ref"
    assert reference["object_key"].endswith(".json")
    assert "storage_encoding" not in reference


async def test_load_coco_rle_gzip_rejects_digest_mismatch(db_session):
    """gzip path still enforces the digest check over decompressed canonical."""
    storage = _storage()
    reference = await store_coco_rle_gzip(RLE, storage=storage, db=db_session)
    # Swap in a gzip body whose decompressed canonical has a different counts
    # array → its sha256 won't match the reference.
    other = {"encoding": "coco_rle", "size": [2, 3], "counts": [6]}
    other_canonical = canonical_rle_bytes(other)
    stream = BytesIO(compress_mask_gzip(other_canonical))
    stream.close = MagicMock()
    owner = await mask_owner_for(db_session, reference)
    storage.objects[owner.storage_ref["object_key"]] = stream.getvalue()
    storage.open_body = lambda _raw: stream
    with pytest.raises(RasterObjectStorageError) as rejected:
        await load_coco_rle(reference, storage=storage, db=db_session)
    assert rejected.value.code == "raster_object_storage_integrity_mismatch"


async def test_load_coco_rle_gzip_rejects_over_uncompressed(db_session):
    """An oversized replacement fails the independent stored receipt first."""
    storage = _storage()
    reference, key = await ready_mask_owner(db_session, storage, RLE, compressed=True)
    big = b"x" * (MAX_UNCOMPRESSED_BYTES + 1)
    corrupted = compress_mask_gzip(big)
    storage.objects[key] = corrupted
    with pytest.raises(RasterObjectStorageError) as rejected:
        await load_coco_rle(reference, storage=storage, db=db_session)
    assert rejected.value.code == "raster_object_storage_integrity_mismatch"
    storage.client.get_object.assert_not_called()
    assert storage.objects[key] == corrupted


def test_canonical_rle_bytes_gzip_pairs_match_reference():
    """canonical_rle_bytes_gzip returns (canonical, gzip) consistent with
    the digest / bytes recorded in build_rle_gzip_reference."""
    canonical, gzip_bytes = canonical_rle_bytes_gzip(RLE)
    digest = hashlib.sha256(canonical).hexdigest()
    # The two are consistent: decompressing gzip_bytes gives canonical.
    assert decompress_mask_gzip(gzip_bytes) == canonical
    assert len(canonical) <= MAX_UNCOMPRESSED_BYTES
    # The digest is deterministic.
    assert digest == hashlib.sha256(canonical_rle_bytes(RLE)).hexdigest()


def test_load_and_store_coco_rle_are_coroutines():
    """async wrappers are real coroutines (so boto3 I/O doesn't block the loop)."""
    assert inspect.iscoroutinefunction(load_coco_rle)
    assert inspect.iscoroutinefunction(store_coco_rle)
    assert inspect.iscoroutinefunction(store_coco_rle_gzip)


def test_decompress_mask_gzip_respects_custom_limits():
    """callers can tighten the limits (used by load path with MAX_RLE_OBJECT_BYTES)."""
    data = b"hello world" * 100  # ~1100 bytes
    gz = compress_mask_gzip(data)
    # Tight uncompressed cap below len(data) → rejected.
    with pytest.raises(ValueError, match="MAX_UNCOMPRESSED_BYTES"):
        decompress_mask_gzip(gz, max_uncompressed=100)
    # Tight ratio cap → rejected (compressed ~30 bytes, decompressed 1100).
    with pytest.raises(ValueError, match="MAX_EXPANSION_RATIO"):
        decompress_mask_gzip(gz, max_ratio=2)
