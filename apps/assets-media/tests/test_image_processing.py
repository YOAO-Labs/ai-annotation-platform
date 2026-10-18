from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from PIL import Image, ImageCms, ImageOps
import pytest

from aap_assets_images.geometry import (
    expected_tile_dimensions,
    pyramid_levels,
    sha256_digest,
)
from aap_assets_images.policy import ImagePolicy
from aap_assets_images_runtime.processing import (
    CHILD_REQUEST_SCHEMA,
    CHILD_RESULT_SCHEMA,
    ImageProcessingError,
    build_pyramid,
    build_thumbnail,
    image_dimensions_from_bytes,
    run_child,
    verify_pyramid_tiles,
)


def _image_bytes(size: tuple[int, int], mode: str = "RGB") -> bytes:
    image = Image.new(mode, size)
    channels = len(image.getbands())
    for y in range(size[1]):
        for x in range(size[0]):
            values = ((x * 17) % 256, (y * 23) % 256, ((x + y) * 31) % 256)
            if channels == 4:
                value = (*values, (x * 11 + y * 7) % 256)
            elif channels == 1:
                value = (values[0],)
            else:
                value = values[:channels]
            image.putpixel((x, y), value)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _legacy_thumbnail(source_path) -> tuple[bytes, str]:
    """The existing API worker algorithm, retained as a byte-for-byte oracle."""
    import blurhash

    with Image.open(source_path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        width, height = image.size
        scale = min(256 / width, 256 / height)
        thumbnail = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.LANCZOS,
        )
        small = image.resize((32, 32), Image.Resampling.LANCZOS)
        digest = blurhash.encode(small, x_components=4, y_components=3)
        encoded = io.BytesIO()
        thumbnail.save(encoded, format="WEBP", quality=80)
        return encoded.getvalue(), digest


def _require_vips():
    try:
        import pyvips
    except (ImportError, OSError):
        pytest.skip("libvips is unavailable in this local test environment")
    return pyvips


def _srgb_profile(tmp_path) -> tuple[bytes, object]:
    profile_bytes = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    profile_path = tmp_path / "srgb.icc"
    profile_path.write_bytes(profile_bytes)
    return profile_bytes, profile_path


def test_thumbnail_matches_current_pillow_and_blurhash_bytes(tmp_path):
    source_path = tmp_path / "small.png"
    source_path.write_bytes(_image_bytes((19, 11), "RGBA"))
    expected_bytes, expected_blurhash = _legacy_thumbnail(source_path)

    result = build_thumbnail(
        source_path,
        policy=ImagePolicy(),
        expected_dimensions=(19, 11),
        deadline=time.monotonic() + 10,
    )

    assert result.webp_bytes == expected_bytes
    assert result.blurhash == expected_blurhash
    assert (result.width, result.height) == (19, 11)
    assert result.source_bytes == source_path.stat().st_size
    with Image.open(io.BytesIO(result.webp_bytes)) as thumbnail:
        assert thumbnail.format == "WEBP"
        assert thumbnail.size == (256, 148)


def test_local_child_cli_writes_thumbnail_and_returns_strict_metadata(tmp_path):
    source_path = tmp_path / "source.png"
    source_data = _image_bytes((19, 11))
    source_path.write_bytes(source_data)
    output_path = tmp_path / "child" / "thumbnail.webp"
    policy = ImagePolicy()
    request = {
        "schema": CHILD_REQUEST_SCHEMA,
        "action": "thumbnail",
        "source_path": str(source_path),
        "source_sha256": hashlib.sha256(source_data).hexdigest(),
        "expected_dimensions": [19, 11],
        "policy": policy.model_dump(mode="json"),
        "deadline": time.monotonic() + 10,
        "output_path": str(output_path),
    }

    package_src = Path(__file__).resolve().parents[1] / "src"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(package_src), environment.get("PYTHONPATH", "")) if part
    )
    result = subprocess.run(
        [sys.executable, "-m", "aap_assets_images_runtime.processing"],
        cwd=package_src.parent,
        env=environment,
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    response = json.loads(result.stdout)
    assert response["schema"] == CHILD_RESULT_SCHEMA
    assert response["action"] == "thumbnail"
    assert response["source_sha256"] == request["source_sha256"]
    assert response["output_bytes"] == output_path.stat().st_size
    assert (
        response["output_sha256"]
        == hashlib.sha256(output_path.read_bytes()).hexdigest()
    )
    with pytest.raises(ImageProcessingError, match="invalid_request"):
        run_child({**request, "unrecognized": True})


def test_dimension_reader_honors_exif_orientation_and_header_budget():
    image = Image.new("RGB", (7, 11), "red")
    exif = image.getexif()
    exif[274] = 6
    payload = io.BytesIO()
    image.save(payload, format="JPEG", exif=exif)
    data = payload.getvalue()

    assert image_dimensions_from_bytes(data) == (11, 7)
    assert image_dimensions_from_bytes(data + b"x" * (300 * 1024)) == (11, 7)
    assert image_dimensions_from_bytes(b"not an image") is None


def test_thumbnail_rejects_dimension_drift_and_expired_deadline(tmp_path):
    source_path = tmp_path / "source.png"
    source_path.write_bytes(_image_bytes((8, 5)))

    with pytest.raises(ImageProcessingError) as mismatch:
        build_thumbnail(
            source_path,
            policy=ImagePolicy(),
            expected_dimensions=(5, 8),
        )
    assert mismatch.value.code == "dimension_mismatch"

    with pytest.raises(ImageProcessingError) as timeout:
        build_thumbnail(
            source_path, policy=ImagePolicy(), deadline=time.monotonic() - 1
        )
    assert timeout.value.code == "timeout"

    with pytest.raises(ImageProcessingError) as source_changed:
        build_thumbnail(
            source_path,
            policy=ImagePolicy(),
            expected_source_sha256="0" * 64,
        )
    assert source_changed.value.code == "source_changed"


def test_thumbnail_obeys_the_captured_source_byte_budget(tmp_path):
    source_path = tmp_path / "source.png"
    source_path.write_bytes(_image_bytes((8, 5)))
    policy = ImagePolicy(max_source_bytes=1)

    with pytest.raises(ImageProcessingError) as captured:
        build_thumbnail(source_path, policy=policy)
    assert captured.value.code == "resource_limit"


def test_pyramid_tile_verifier_rejects_missing_corrupt_and_extra_tiles(tmp_path):
    width, height = 513, 257
    levels = pyramid_levels(width, height)
    max_level = len(levels) - 1
    tile_root = tmp_path / "tiles"
    tile_root.mkdir()
    paths = []
    for level in levels:
        dzi_level = max_level - level["level"]
        level_dir = tile_root / str(dzi_level)
        for y in range(level["rows"]):
            for x in range(level["columns"]):
                path = level_dir / f"{x}_{y}.webp"
                path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", expected_tile_dimensions(level, x, y), "navy").save(
                    path, format="WEBP"
                )
                paths.append(path)

    tiles = verify_pyramid_tiles(tile_root, width=width, height=height)
    assert (
        len(tiles)
        == len(paths)
        == sum(level["columns"] * level["rows"] for level in levels)
    )

    paths[0].unlink()
    with pytest.raises(ImageProcessingError, match="tile_grid_mismatch"):
        verify_pyramid_tiles(tile_root, width=width, height=height)
    Image.new("RGB", expected_tile_dimensions(levels[0], 0, 0), "navy").save(
        paths[0], format="WEBP"
    )
    paths[0].write_bytes(b"corrupt webp")
    with pytest.raises(ImageProcessingError, match="tile_grid_mismatch"):
        verify_pyramid_tiles(tile_root, width=width, height=height)
    Image.new("RGB", expected_tile_dimensions(levels[0], 0, 0), "navy").save(
        paths[0], format="WEBP"
    )
    extra = tile_root / "unexpected" / "extra.webp"
    extra.parent.mkdir()
    Image.new("RGB", (1, 1), "black").save(extra, format="WEBP")
    with pytest.raises(ImageProcessingError, match="tile_grid_mismatch"):
        verify_pyramid_tiles(tile_root, width=width, height=height)


def test_build_pyramid_preserves_alpha_and_returns_verified_manifest(tmp_path):
    _require_vips()
    source_path = tmp_path / "alpha.png"
    source_path.write_bytes(_image_bytes((513, 257), "RGBA"))
    _profile, profile_path = _srgb_profile(tmp_path)

    result = build_pyramid(
        source_path,
        tmp_path / "output",
        generation=1,
        expected_dimensions=(513, 257),
        policy=ImagePolicy(),
        srgb_profile=profile_path,
        deadline=time.monotonic() + 60,
    )

    manifest = json.loads(result.manifest_bytes)
    assert (result.width, result.height) == (513, 257)
    assert manifest["schema"] == "aap-image-pyramid/v1"
    assert manifest["overview"]["contentDigest"] == result.overview_digest
    assert result.manifest_digest == sha256_digest(result.manifest_bytes)
    assert result.tile_count == len(result.tiles)
    with Image.open(result.overview_path) as overview:
        assert overview.format == "WEBP"
        assert "A" in overview.getbands()


def test_build_pyramid_autorotates_exif_and_converts_grayscale_and_icc(tmp_path):
    _require_vips()
    profile_bytes, profile_path = _srgb_profile(tmp_path)

    exif_source = tmp_path / "rotated.jpg"
    exif_image = Image.new("RGB", (513, 257), "navy")
    exif = exif_image.getexif()
    exif[274] = 6
    exif_image.save(exif_source, format="JPEG", exif=exif)
    rotated = build_pyramid(
        exif_source,
        tmp_path / "rotated-output",
        generation=2,
        expected_dimensions=(257, 513),
        policy=ImagePolicy(),
        srgb_profile=profile_path,
        deadline=time.monotonic() + 60,
    )
    assert (rotated.width, rotated.height) == (257, 513)

    icc_source = tmp_path / "gray-icc.png"
    Image.new("L", (513, 257), 128).save(
        icc_source, format="PNG", icc_profile=profile_bytes
    )
    normalized = build_pyramid(
        icc_source,
        tmp_path / "gray-output",
        generation=3,
        expected_dimensions=(513, 257),
        policy=ImagePolicy(),
        srgb_profile=profile_path,
        deadline=time.monotonic() + 60,
    )
    assert (normalized.width, normalized.height) == (513, 257)
    assert normalized.tile_count > 0


def test_build_pyramid_rejects_multipage_sources(tmp_path):
    _require_vips()
    source_path = tmp_path / "two-pages.tiff"
    first = Image.new("RGB", (8, 4), "red")
    second = Image.new("RGB", (8, 4), "blue")
    first.save(source_path, format="TIFF", save_all=True, append_images=[second])
    _profile, profile_path = _srgb_profile(tmp_path)

    with pytest.raises(ImageProcessingError) as captured:
        build_pyramid(
            source_path,
            tmp_path / "output",
            generation=1,
            expected_dimensions=None,
            policy=ImagePolicy(),
            srgb_profile=profile_path,
            deadline=time.monotonic() + 60,
        )
    assert captured.value.code == "multi_page_unsupported"


def test_build_pyramid_rejects_source_sha_before_loading_vips(tmp_path):
    source_path = tmp_path / "source.png"
    source_path.write_bytes(_image_bytes((8, 4)))

    with pytest.raises(ImageProcessingError) as captured:
        build_pyramid(
            source_path,
            tmp_path / "output",
            generation=1,
            policy=ImagePolicy(),
            srgb_profile=tmp_path / "unused.icc",
            deadline=time.monotonic() + 10,
            expected_source_sha256="0" * 64,
        )
    assert captured.value.code == "source_changed"
