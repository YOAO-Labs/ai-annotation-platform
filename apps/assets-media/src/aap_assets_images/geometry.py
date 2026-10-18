"""Pure geometry and manifest helpers for image pyramid artifacts."""

from __future__ import annotations

import hashlib
import json
import math
from uuid import UUID

from aap_assets_images.policy import ImagePolicy

PYRAMID_SCHEMA = "aap-image-pyramid/v1"
PYRAMID_TILE_SIZE = 512
PYRAMID_OVERLAP = 1
PYRAMID_FORMAT = "webp"
PYRAMID_PREFIX = "image-pyramids"


class ImageGeometryError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def pixels_for_dimensions(width: int | None, height: int | None) -> int | None:
    if (
        width is None
        or height is None
        or isinstance(width, bool)
        or isinstance(height, bool)
        or width <= 0
        or height <= 0
    ):
        return None
    return width * height


def pyramid_required(
    width: int | None, height: int | None, *, policy: ImagePolicy
) -> bool:
    pixels = pixels_for_dimensions(width, height)
    return pixels is not None and pixels >= policy.required_pixels


def pyramid_eligible(
    width: int | None, height: int | None, *, policy: ImagePolicy
) -> bool:
    pixels = pixels_for_dimensions(width, height)
    return pixels is not None and pixels >= policy.optional_pixels


def pyramid_levels(width: int, height: int) -> list[dict[str, int]]:
    """Full-resolution-first powers-of-two grid with explicit ceil rounding."""
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or width <= 0
        or height <= 0
    ):
        raise ImageGeometryError("unsupported_dimensions")

    levels: list[dict[str, int]] = []
    level = 0
    while True:
        scale = 1 << level
        level_width = max(1, math.ceil(width / scale))
        level_height = max(1, math.ceil(height / scale))
        levels.append(
            {
                "level": level,
                "scaleFactor": scale,
                "width": level_width,
                "height": level_height,
                "columns": math.ceil(level_width / PYRAMID_TILE_SIZE),
                "rows": math.ceil(level_height / PYRAMID_TILE_SIZE),
            }
        )
        if level_width == 1 and level_height == 1:
            return levels
        level += 1


def total_tile_count(width: int, height: int) -> int:
    return sum(
        level["columns"] * level["rows"] for level in pyramid_levels(width, height)
    )


def validate_dimensions(width: int, height: int, *, policy: ImagePolicy) -> None:
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or width <= 0
        or height <= 0
    ):
        raise ImageGeometryError("unsupported_dimensions")
    if (
        width > policy.max_dimension
        or height > policy.max_dimension
        or width * height > policy.max_pixels
    ):
        raise ImageGeometryError("unsupported_dimensions")
    if total_tile_count(width, height) > policy.max_tiles:
        raise ImageGeometryError("resource_limit")


def expected_tile_dimensions(level: dict[str, int], x: int, y: int) -> tuple[int, int]:
    if not (0 <= x < level["columns"] and 0 <= y < level["rows"]):
        raise ImageGeometryError("invalid_coordinate")
    core_width = min(PYRAMID_TILE_SIZE, level["width"] - x * PYRAMID_TILE_SIZE)
    core_height = min(PYRAMID_TILE_SIZE, level["height"] - y * PYRAMID_TILE_SIZE)
    stored_width = core_width + PYRAMID_OVERLAP * (
        int(x > 0) + int(x + 1 < level["columns"])
    )
    stored_height = core_height + PYRAMID_OVERLAP * (
        int(y > 0) + int(y + 1 < level["rows"])
    )
    return int(stored_width), int(stored_height)


def generation_prefix(asset_id: UUID | str, generation: int) -> str:
    if isinstance(generation, bool) or generation < 1:
        raise ImageGeometryError("invalid_generation")
    return f"{PYRAMID_PREFIX}/{asset_id}/g{generation}"


def tile_key(asset_id: UUID | str, generation: int, level: int, x: int, y: int) -> str:
    return f"{generation_prefix(asset_id, generation)}/tiles/{level}/{x}/{y}.webp"


def overview_key(asset_id: UUID | str, generation: int) -> str:
    return f"{generation_prefix(asset_id, generation)}/overview.webp"


def manifest_key(asset_id: UUID | str, generation: int) -> str:
    return f"{generation_prefix(asset_id, generation)}/manifest.json"


def build_manifest(
    *,
    generation: int,
    source_fingerprint: str,
    width: int,
    height: int,
    overview_width: int,
    overview_height: int,
    overview_digest: str,
    policy: ImagePolicy,
) -> dict[str, object]:
    return {
        "schema": PYRAMID_SCHEMA,
        "generation": generation,
        "sourceFingerprint": source_fingerprint,
        "normalizationVersion": policy.normalization_version,
        "width": width,
        "height": height,
        "tileSize": PYRAMID_TILE_SIZE,
        "overlap": PYRAMID_OVERLAP,
        "format": PYRAMID_FORMAT,
        "levels": pyramid_levels(width, height),
        "overview": {
            "width": overview_width,
            "height": overview_height,
            "contentDigest": overview_digest,
        },
    }


def canonical_manifest_bytes(manifest: dict[str, object]) -> bytes:
    return json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def sha256_digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"
