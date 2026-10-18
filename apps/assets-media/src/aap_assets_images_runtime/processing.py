"""Local-only image decoding and artifact generation for the Assets image owner.

This module does no S3, database, HTTP or worker coordination. The caller owns
the captured source identity, output directory lifetime and subprocess budget.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any

from aap_assets_images.policy import ImagePolicy
from aap_assets_images.geometry import (
    ImageGeometryError,
    PYRAMID_OVERLAP,
    PYRAMID_TILE_SIZE,
    build_manifest,
    canonical_manifest_bytes,
    expected_tile_dimensions,
    pyramid_levels,
    sha256_digest,
    validate_dimensions,
)

IMAGE_HEADER_BYTES = 256 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
CHILD_REQUEST_SCHEMA = "aap-assets-images/local-child-request/v1"
CHILD_RESULT_SCHEMA = "aap-assets-images/local-child-result/v1"


class ImageProcessingError(RuntimeError):
    """A safe, stable local-processing failure code for the owner worker."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ThumbnailResult:
    width: int
    height: int
    webp_bytes: bytes
    blurhash: str
    source_bytes: int
    source_sha256: str


@dataclass(frozen=True, slots=True)
class PyramidTile:
    path: Path
    level: int
    x: int
    y: int
    width: int
    height: int
    byte_size: int


@dataclass(frozen=True, slots=True)
class PyramidResult:
    source_fingerprint: str
    width: int
    height: int
    max_level: int
    overview_path: Path
    manifest_path: Path
    overview_width: int
    overview_height: int
    overview_digest: str
    tiles: tuple[PyramidTile, ...]
    tile_count: int
    manifest_bytes: bytes
    manifest_digest: str
    retained_bytes: int
    temp_bytes: int


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise ImageProcessingError("timeout")


def _logical_dimensions(image) -> tuple[int, int]:
    width, height = int(image.width), int(image.height)
    try:
        orientation = int(image.getexif().get(274, 1))
    except Exception:  # noqa: BLE001 - malformed EXIF means orientation 1
        orientation = 1
    if orientation in {5, 6, 7, 8}:
        return height, width
    return width, height


def image_dimensions_from_bytes(data: bytes) -> tuple[int, int] | None:
    """Read logical dimensions from at most the existing 256 KiB header range."""
    if not isinstance(data, bytes) or not data:
        return None
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data[:IMAGE_HEADER_BYTES])) as image:
            return _logical_dimensions(image)
    except Exception:  # noqa: BLE001 - unsupported/truncated headers stay unknown
        return None


def build_thumbnail(
    source: Path,
    *,
    policy: ImagePolicy,
    expected_dimensions: tuple[int, int] | None = None,
    expected_source_sha256: str | None = None,
    deadline: float | None = None,
) -> ThumbnailResult:
    """Port the current small-image WEBP and blurhash bytes exactly."""
    source = Path(source)
    _check_deadline(deadline)
    try:
        source_bytes = source.stat().st_size
    except OSError as exc:
        raise ImageProcessingError("source_missing") from exc
    if source_bytes <= 0:
        raise ImageProcessingError("source_missing")
    if source_bytes > min(policy.thumbnail_max_source_bytes, policy.max_source_bytes):
        raise ImageProcessingError("resource_limit")
    source_sha256 = _sha256_hex_file(source, deadline=deadline)
    if expected_source_sha256 is not None:
        if _SHA256_RE.fullmatch(expected_source_sha256) is None:
            raise ImageProcessingError("invalid_request")
        if source_sha256 != expected_source_sha256:
            raise ImageProcessingError("source_changed")

    try:
        from PIL import Image, ImageOps
        import blurhash as bh

        with Image.open(source) as raw:
            image = ImageOps.exif_transpose(raw).convert("RGB")
            width, height = (int(image.width), int(image.height))
            if expected_dimensions is not None and expected_dimensions != (
                width,
                height,
            ):
                raise ImageProcessingError("dimension_mismatch")
            max_side = 256
            scale = min(max_side / width, max_side / height)
            thumbnail = image.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                Image.Resampling.LANCZOS,
            )
            small = image.resize((32, 32), Image.Resampling.LANCZOS)
            blurhash_value = bh.encode(small, x_components=4, y_components=3)
            output = io.BytesIO()
            thumbnail.save(output, format="WEBP", quality=80)
    except ImageProcessingError:
        raise
    except Exception as exc:  # noqa: BLE001 - Pillow/blurhash use varied errors
        raise ImageProcessingError("decode_failed") from exc

    _check_deadline(deadline)
    return ThumbnailResult(
        width=width,
        height=height,
        webp_bytes=output.getvalue(),
        blurhash=blurhash_value,
        source_bytes=source_bytes,
        source_sha256=source_sha256,
    )


def _sha256_hex_file(path: Path, *, deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
                _check_deadline(deadline)
    except ImageProcessingError:
        raise
    except OSError as exc:
        raise ImageProcessingError("source_missing") from exc
    return digest.hexdigest()


def _sha256_file(path: Path, *, deadline: float | None = None) -> str:
    return f"sha256:{_sha256_hex_file(path, deadline=deadline)}"


def _directory_size(
    directory: Path,
    *,
    limit: int,
    deadline: float | None,
) -> int:
    total = 0
    for path in directory.rglob("*"):
        _check_deadline(deadline)
        if path.is_symlink():
            raise ImageProcessingError("tile_grid_mismatch")
        if path.is_file():
            total += path.stat().st_size
            if total > limit:
                raise ImageProcessingError("resource_limit")
    return total


def _normalized_image(source: Path, *, policy: ImagePolicy, srgb_profile: Path):
    os.environ["VIPS_CONCURRENCY"] = str(policy.vips_concurrency)
    try:
        import pyvips
    except (ImportError, OSError) as exc:
        raise ImageProcessingError("generator_unavailable") from exc

    pyvips.cache_set_max(64)
    pyvips.cache_set_max_mem(64 * 1024 * 1024)
    pyvips.cache_set_max_files(16)
    try:
        image = pyvips.Image.new_from_file(
            str(source), access="sequential", fail_on="error"
        )
        if image.get_typeof("n-pages") and int(image.get("n-pages")) > 1:
            raise ImageProcessingError("multi_page_unsupported")
        image = image.autorot()
        if image.get_typeof("icc-profile-data"):
            if not srgb_profile.is_file():
                raise ImageProcessingError("color_profile_unavailable")
            image = image.icc_transform(str(srgb_profile), embedded=True)
        elif image.interpretation != "srgb":
            image = image.colourspace("srgb")
        return image
    except ImageProcessingError:
        raise
    except Exception as exc:  # noqa: BLE001 - libvips exposes one generic error
        raise ImageProcessingError("decode_failed") from exc


def verify_pyramid_tiles(
    tile_root: Path,
    *,
    width: int,
    height: int,
    deadline: float | None = None,
) -> tuple[PyramidTile, ...]:
    """Verify every expected WEBP tile and reject missing or extra grid files."""
    try:
        from PIL import Image
    except ImportError as exc:  # Pillow is a declared API dependency
        raise ImageProcessingError("generator_unavailable") from exc

    levels = pyramid_levels(width, height)
    max_level = len(levels) - 1
    verified: list[PyramidTile] = []
    for level in levels:
        dzi_level = max_level - level["level"]
        level_dir = tile_root / str(dzi_level)
        for y in range(level["rows"]):
            for x in range(level["columns"]):
                _check_deadline(deadline)
                path = level_dir / f"{x}_{y}.webp"
                if path.is_symlink() or not path.is_file():
                    raise ImageProcessingError("tile_grid_mismatch")
                expected = expected_tile_dimensions(level, x, y)
                try:
                    with Image.open(path) as image:
                        actual = (int(image.width), int(image.height))
                        if image.format != "WEBP" or actual != expected:
                            raise ImageProcessingError("tile_grid_mismatch")
                        image.load()
                except ImageProcessingError:
                    raise
                except Exception as exc:  # noqa: BLE001 - corrupt tile
                    raise ImageProcessingError("tile_grid_mismatch") from exc
                verified.append(
                    PyramidTile(
                        path=path,
                        level=level["level"],
                        x=x,
                        y=y,
                        width=expected[0],
                        height=expected[1],
                        byte_size=path.stat().st_size,
                    )
                )

    actual_files = [path for path in tile_root.rglob("*.webp") if path.is_file()]
    if len(actual_files) != len(verified):
        raise ImageProcessingError("tile_grid_mismatch")
    return tuple(verified)


def build_pyramid(
    source: Path,
    output_dir: Path,
    *,
    generation: int,
    expected_dimensions: tuple[int, int] | None = None,
    policy: ImagePolicy,
    srgb_profile: Path,
    deadline: float,
    expected_source_sha256: str | None = None,
) -> PyramidResult:
    """Generate, verify and manifest a complete local libvips pyramid."""
    source = Path(source)
    output_dir = Path(output_dir)
    srgb_profile = Path(srgb_profile)
    _check_deadline(deadline)
    try:
        source_bytes = source.stat().st_size
    except OSError as exc:
        raise ImageProcessingError("source_missing") from exc
    if source_bytes <= 0:
        raise ImageProcessingError("source_missing")
    if source_bytes > policy.max_source_bytes:
        raise ImageProcessingError("resource_limit")
    if (
        expected_source_sha256 is not None
        and _SHA256_RE.fullmatch(expected_source_sha256) is None
    ):
        raise ImageProcessingError("invalid_request")
    source_sha256 = _sha256_hex_file(source, deadline=deadline)
    if expected_source_sha256 is not None and source_sha256 != expected_source_sha256:
        raise ImageProcessingError("source_changed")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise ImageProcessingError("invalid_generation")

    try:
        if output_dir.is_symlink():
            raise ImageProcessingError("invalid_request")
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(output_dir.iterdir()):
            raise ImageProcessingError("invalid_request")
        image = _normalized_image(source, policy=policy, srgb_profile=srgb_profile)
        width, height = int(image.width), int(image.height)
        try:
            validate_dimensions(width, height, policy=policy)
        except ImageGeometryError as exc:
            raise ImageProcessingError(exc.code) from None
        if expected_dimensions is not None and expected_dimensions != (width, height):
            raise ImageProcessingError("dimension_mismatch")

        base = output_dir / "pyramid"
        image.dzsave(
            str(base),
            layout="dz",
            depth="onepixel",
            tile_size=PYRAMID_TILE_SIZE,
            overlap=PYRAMID_OVERLAP,
            suffix=".webp[Q=88,strip]",
        )
        levels = pyramid_levels(width, height)
        overview_level = next(
            level
            for level in levels
            if level["width"] <= 512 and level["height"] <= 512
        )
        dzi_level = len(levels) - 1 - overview_level["level"]
        tile_root = Path(str(base) + "_files")
        overview_source = tile_root / str(dzi_level) / "0_0.webp"
        overview_path = output_dir / "overview.webp"
        shutil.copyfile(overview_source, overview_path)
    except ImageProcessingError:
        raise
    except Exception as exc:  # noqa: BLE001 - libvips/filesystem failures
        raise ImageProcessingError("encode_failed") from exc

    tiles = verify_pyramid_tiles(
        tile_root, width=width, height=height, deadline=deadline
    )
    overview_digest = _sha256_file(overview_path, deadline=deadline)
    source_fingerprint = f"sha256:{source_sha256}"
    manifest = build_manifest(
        generation=generation,
        source_fingerprint=source_fingerprint,
        width=width,
        height=height,
        overview_width=overview_level["width"],
        overview_height=overview_level["height"],
        overview_digest=overview_digest,
        policy=policy,
    )
    manifest_bytes = canonical_manifest_bytes(manifest)
    manifest_digest = sha256_digest(manifest_bytes)
    manifest_path = output_dir / "manifest.json"
    try:
        with manifest_path.open("xb") as stream:
            stream.write(manifest_bytes)
    except OSError as exc:
        raise ImageProcessingError("encode_failed") from exc
    generated_bytes = _directory_size(
        output_dir, limit=policy.max_derived_bytes, deadline=deadline
    )
    temp_bytes = source_bytes + generated_bytes
    if temp_bytes > policy.max_temp_bytes:
        raise ImageProcessingError("resource_limit")
    retained_bytes = sum(tile.byte_size for tile in tiles)
    retained_bytes += overview_path.stat().st_size + len(manifest_bytes)
    if retained_bytes > policy.max_derived_bytes:
        raise ImageProcessingError("resource_limit")
    _check_deadline(deadline)

    return PyramidResult(
        source_fingerprint=source_fingerprint,
        width=width,
        height=height,
        max_level=len(levels) - 1,
        overview_path=overview_path,
        manifest_path=manifest_path,
        overview_width=overview_level["width"],
        overview_height=overview_level["height"],
        overview_digest=overview_digest,
        tiles=tiles,
        tile_count=len(tiles),
        manifest_bytes=manifest_bytes,
        manifest_digest=manifest_digest,
        retained_bytes=retained_bytes,
        temp_bytes=temp_bytes,
    )


def _require_exact_keys(value: Any, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ImageProcessingError("invalid_request")
    return value


def _absolute_path(value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise ImageProcessingError("invalid_request")
    path = Path(value)
    if not path.is_absolute():
        raise ImageProcessingError("invalid_request")
    return path


def _expected_dimensions(value: Any) -> tuple[int, int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(part, bool) or not isinstance(part, int) for part in value)
    ):
        raise ImageProcessingError("invalid_request")
    if value[0] <= 0 or value[1] <= 0:
        raise ImageProcessingError("invalid_request")
    return value[0], value[1]


def _source_sha256(value: Any) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ImageProcessingError("invalid_request")
    return value


def _child_deadline(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ImageProcessingError("invalid_request")
    return float(value)


def run_child(request: Any) -> dict[str, Any]:
    """Execute one strictly shaped local request for a bounded worker child."""
    if not isinstance(request, dict) or request.get("schema") != CHILD_REQUEST_SCHEMA:
        raise ImageProcessingError("invalid_request")
    action = request.get("action")
    common = {
        "schema",
        "action",
        "source_path",
        "source_sha256",
        "expected_dimensions",
        "policy",
        "deadline",
    }
    if action == "thumbnail":
        body = _require_exact_keys(request, common | {"output_path"})
        output_path = _absolute_path(body["output_path"])
        try:
            policy = ImagePolicy.model_validate(body["policy"])
        except Exception as exc:  # noqa: BLE001 - strict local request boundary
            raise ImageProcessingError("invalid_request") from exc
        result = build_thumbnail(
            _absolute_path(body["source_path"]),
            policy=policy,
            expected_dimensions=_expected_dimensions(body["expected_dimensions"]),
            expected_source_sha256=_source_sha256(body["source_sha256"]),
            deadline=_child_deadline(body["deadline"]),
        )
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with output_path.open("xb") as stream:
                stream.write(result.webp_bytes)
        except OSError as exc:
            raise ImageProcessingError("encode_failed") from exc
        return {
            "schema": CHILD_RESULT_SCHEMA,
            "action": "thumbnail",
            "width": result.width,
            "height": result.height,
            "blurhash": result.blurhash,
            "source_bytes": result.source_bytes,
            "source_sha256": result.source_sha256,
            "output_path": str(output_path),
            "output_bytes": len(result.webp_bytes),
            "output_sha256": _sha256_hex_file(output_path),
        }
    if action == "pyramid":
        body = _require_exact_keys(
            request, common | {"output_dir", "generation", "srgb_profile"}
        )
        generation = body["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise ImageProcessingError("invalid_request")
        try:
            policy = ImagePolicy.model_validate(body["policy"])
        except Exception as exc:  # noqa: BLE001 - strict local request boundary
            raise ImageProcessingError("invalid_request") from exc
        result = build_pyramid(
            _absolute_path(body["source_path"]),
            _absolute_path(body["output_dir"]),
            generation=generation,
            expected_dimensions=_expected_dimensions(body["expected_dimensions"]),
            policy=policy,
            srgb_profile=_absolute_path(body["srgb_profile"]),
            deadline=_child_deadline(body["deadline"]),
            expected_source_sha256=_source_sha256(body["source_sha256"]),
        )
        return {
            "schema": CHILD_RESULT_SCHEMA,
            "action": "pyramid",
            "source_fingerprint": result.source_fingerprint,
            "width": result.width,
            "height": result.height,
            "max_level": result.max_level,
            "overview_path": str(result.overview_path),
            "overview_width": result.overview_width,
            "overview_height": result.overview_height,
            "overview_digest": result.overview_digest,
            "manifest_path": str(result.manifest_path),
            "manifest_digest": result.manifest_digest,
            "tile_count": result.tile_count,
            "tiles": [
                {
                    "path": str(tile.path),
                    "level": tile.level,
                    "x": tile.x,
                    "y": tile.y,
                    "width": tile.width,
                    "height": tile.height,
                    "byte_size": tile.byte_size,
                }
                for tile in result.tiles
            ],
            "retained_bytes": result.retained_bytes,
            "temp_bytes": result.temp_bytes,
        }
    raise ImageProcessingError("invalid_request")


def main() -> int:
    """Child CLI: one bounded JSON request in, one metadata reply out."""
    try:

        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ImageProcessingError("invalid_request")
                value[key] = item
            return value

        request = json.loads(sys.stdin.read(), object_pairs_hook=unique_object)
        response = run_child(request)
    except ImageProcessingError as exc:
        sys.stderr.write(json.dumps({"code": exc.code}, separators=(",", ":")) + "\n")
        return 2
    except Exception:  # noqa: BLE001 - do not leak local paths or source data
        sys.stderr.write('{"code":"invalid_response"}\n')
        return 3
    json.dump(response, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
