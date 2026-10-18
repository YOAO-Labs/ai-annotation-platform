from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from aap_assets_images.geometry import (
    ImageGeometryError,
    build_manifest,
    canonical_manifest_bytes,
    expected_tile_dimensions,
    generation_prefix,
    manifest_key,
    overview_key,
    pyramid_eligible,
    pyramid_levels,
    pyramid_required,
    sha256_digest,
    tile_key,
    total_tile_count,
    validate_dimensions,
)
from aap_assets_images.policy import ImagePolicy


def test_pyramid_levels_preserve_full_resolution_first_and_ceil_rounding():
    levels = pyramid_levels(1025, 513)

    assert levels[:3] == [
        {
            "level": 0,
            "scaleFactor": 1,
            "width": 1025,
            "height": 513,
            "columns": 3,
            "rows": 2,
        },
        {
            "level": 1,
            "scaleFactor": 2,
            "width": 513,
            "height": 257,
            "columns": 2,
            "rows": 1,
        },
        {
            "level": 2,
            "scaleFactor": 4,
            "width": 257,
            "height": 129,
            "columns": 1,
            "rows": 1,
        },
    ]
    assert levels[-1]["width"] == levels[-1]["height"] == 1
    assert total_tile_count(1025, 513) == sum(
        level["columns"] * level["rows"] for level in levels
    )
    assert expected_tile_dimensions(levels[0], 0, 0) == (513, 513)
    assert expected_tile_dimensions(levels[0], 1, 0) == (514, 513)
    assert expected_tile_dimensions(levels[0], 2, 1) == (2, 2)


def test_pyramid_thresholds_keep_the_captured_policy_boundaries():
    policy = ImagePolicy()

    assert not pyramid_eligible(None, 4096, policy=policy)
    assert not pyramid_eligible(4095, 4096, policy=policy)
    assert pyramid_eligible(4096, 4096, policy=policy)
    assert not pyramid_required(9999, 5000, policy=policy)
    assert pyramid_required(10_000, 5000, policy=policy)
    validate_dimensions(4096, 4096, policy=policy)


@pytest.mark.parametrize(
    ("width", "height", "code"),
    [
        (0, 1, "unsupported_dimensions"),
        (True, 2, "unsupported_dimensions"),
        (32_769, 1, "unsupported_dimensions"),
        (20_000, 20_000, "unsupported_dimensions"),
        (4096, 4096, "resource_limit"),
    ],
)
def test_dimension_validation_rejects_unsupported_or_over_tiled_inputs(
    width, height, code
):
    policy = ImagePolicy(max_tiles=1)
    with pytest.raises(ImageGeometryError) as captured:
        validate_dimensions(width, height, policy=policy)
    assert captured.value.code == code


def test_manifest_is_canonical_and_keeps_storage_private():
    manifest = build_manifest(
        generation=4,
        source_fingerprint="sha256:source",
        width=1025,
        height=513,
        overview_width=512,
        overview_height=256,
        overview_digest="sha256:overview",
        policy=ImagePolicy(),
    )
    encoded = canonical_manifest_bytes(manifest)

    assert manifest["schema"] == "aap-image-pyramid/v1"
    assert manifest["generation"] == 4
    assert manifest["normalizationVersion"] == "exif-autorotate-srgb-v1"
    assert "image-pyramids/" not in encoded.decode()
    assert "http://" not in encoded.decode()
    assert "https://" not in encoded.decode()
    assert sha256_digest(encoded) == f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def test_pyramid_key_helpers_match_the_legacy_manifest_layout():
    asset_id = "01234567-89ab-cdef-0123-456789abcdef"

    assert generation_prefix(asset_id, 2) == f"image-pyramids/{asset_id}/g2"
    assert tile_key(asset_id, 2, 1, 3, 4) == (
        f"image-pyramids/{asset_id}/g2/tiles/1/3/4.webp"
    )
    assert overview_key(asset_id, 2) == f"image-pyramids/{asset_id}/g2/overview.webp"
    assert manifest_key(asset_id, 2) == f"image-pyramids/{asset_id}/g2/manifest.json"


def test_image_policy_rejects_invalid_threshold_order():
    with pytest.raises(ValidationError):
        ImagePolicy(optional_pixels=2048, required_pixels=1024)
