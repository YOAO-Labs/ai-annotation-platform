from __future__ import annotations

import gzip
import hashlib

import pytest
from pydantic import ValidationError

from aap_assets_mask_content import codec
from aap_assets_mask_content.contract import MaskReference, RleContent


def test_canonical_bytes_and_legacy_identity_reference_match_api_format():
    rle = {"encoding": "coco_rle", "size": [2, 2], "counts": [1, 3]}
    expected = b'{"encoding":"coco_rle","size":[2,2],"counts":[1,3]}'

    assert codec.canonical_rle_bytes(rle) == expected
    reference = codec.build_rle_reference(rle)
    digest = hashlib.sha256(expected).hexdigest()
    assert reference.model_dump(mode="json") == {
        "encoding": "coco_rle_ref",
        "storage_encoding": None,
        "size": [2, 2],
        "object_key": f"raster-masks/sha256/{digest[:2]}/{digest[2:4]}/{digest}.json",
        "sha256": digest,
        "runs": 2,
        "bytes": len(expected),
    }
    assert codec.decode_reference(reference, expected) == RleContent(
        encoding="coco_rle", size=(2, 2), counts=(1, 3)
    )


def test_gzip_reference_keeps_digest_over_uncompressed_canonical_bytes():
    counts = [((index * 7) % 8) + 1 for index in range(500)]
    counts.append(4096 - sum(counts))
    rle = {
        "encoding": "coco_rle",
        "size": [64, 64],
        "counts": counts,
    }
    raw, compressed = codec.canonical_rle_bytes_gzip(rle)
    reference = codec.build_rle_gzip_reference(rle)

    assert gzip.decompress(compressed) == raw
    assert reference.sha256 == hashlib.sha256(raw).hexdigest()
    assert reference.object_key.endswith(".json.gz")
    assert reference.bytes == len(raw)
    assert codec.decode_reference(reference, compressed) == RleContent.model_validate(
        rle
    )


def test_legacy_gzip_reference_without_storage_encoding_remains_readable():
    counts = [((index * 7) % 8) + 1 for index in range(500)]
    counts.append(4096 - sum(counts))
    rle = {"encoding": "coco_rle", "size": [64, 64], "counts": counts}
    _raw, compressed = codec.canonical_rle_bytes_gzip(rle)
    reference = codec.build_rle_gzip_reference(rle).model_dump(mode="json")
    reference["encoding"] = "coco_rle_gzip"
    reference.pop("storage_encoding")

    decoded = codec.decode_reference(reference, compressed)

    assert decoded == RleContent.model_validate(rle)


def test_gzip_expansion_is_bounded_and_identity_fallback_is_available():
    raw = gzip.compress(b"x" * 4096, compresslevel=9)
    with pytest.raises(ValueError, match="expansion ratio"):
        codec.decompress_mask_gzip(raw)

    rle = {
        "encoding": "coco_rle",
        "size": [100, 100],
        "counts": [1] * 1000 + [9000],
    }
    with pytest.raises(ValueError, match="expansion ratio"):
        codec.build_rle_gzip_reference(rle)
    assert codec.build_rle_reference(rle).object_key.endswith(".json")


@pytest.mark.parametrize(
    "rle",
    [
        {"encoding": "coco_rle", "size": [2, 2], "counts": [1, True, 2]},
        {"encoding": "coco_rle", "size": [2, 2], "counts": [1, 2]},
        {"encoding": "other", "size": [2, 2], "counts": [4]},
        {"encoding": "coco_rle", "size": [9000, 1], "counts": [9000]},
    ],
)
def test_invalid_rle_is_rejected_before_serialization(rle):
    with pytest.raises((ValueError, ValidationError)):
        codec.canonical_rle_bytes(rle)


def test_unvalidated_typed_rle_is_rechecked_before_serialization():
    invalid = RleContent.model_construct(encoding="coco_rle", size=(2,), counts=(4,))

    with pytest.raises(ValueError, match="size must be"):
        codec.canonical_rle_bytes(invalid)


def test_reference_validation_rejects_noncanonical_keys_before_storage():
    reference = {
        "encoding": "coco_rle_ref",
        "size": [2, 2],
        "object_key": "raster-masks/sha256/aa/bb/" + "a" * 64 + ".json",
        "sha256": "a" * 64,
        "runs": 2,
        "bytes": 4,
    }
    with pytest.raises(ValidationError):
        MaskReference.model_validate(reference)
