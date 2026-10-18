"""Reproducible libvips image-pyramid correctness/performance probe.

The script creates fixtures only inside a TemporaryDirectory and removes them on
exit. Run it inside the API image so pyvips and the exact production libvips are
measured:

    python scripts/benchmark_image_pyramid.py --case correctness
    python scripts/benchmark_image_pyramid.py --case 8k
    python scripts/benchmark_image_pyramid.py --case 50mp
    python scripts/benchmark_image_pyramid.py --case 200mp
"""

from __future__ import annotations

import os
import argparse
import json
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

os.environ.setdefault(
    "VIPS_CONCURRENCY", os.environ.get("IMAGE_PYRAMID_VIPS_CONCURRENCY", "4")
)

from PIL import Image

from aap_assets_images.policy import image_policy_from_environment
from aap_assets_images_runtime.processing import (
    ImageProcessingError,
    build_pyramid,
)

LARGE_CASES = {
    "8k": (8192, 4096),
    "50mp": (8192, 6144),
    "200mp": (16384, 12288),
}


def _directory_size(root: Path, *, limit: int) -> int:
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
            if total > limit:
                raise RuntimeError("benchmark output exceeded its byte limit")
    return total


def _run_fixture(path: Path, width: int, height: int) -> dict:
    output = path.parent / f"{path.stem}-output-{uuid.uuid4().hex}"
    started = time.monotonic()
    result = build_pyramid(
        path,
        output,
        generation=1,
        expected_dimensions=(width, height),
        policy=image_policy_from_environment(),
        srgb_profile=Path(
            os.environ.get(
                "ASSETS_IMAGES_SRGB_PROFILE",
                os.environ.get(
                    "IMAGE_PYRAMID_SRGB_PROFILE", "/usr/share/color/icc/sRGB.icc"
                ),
            )
        ),
        deadline=time.monotonic() + 1800,
    )
    with Image.open(result.tiles[0].path) as first_tile:
        first_tile_bands = len(first_tile.getbands())
        first_tile_has_alpha = "A" in first_tile.getbands()
    report = {
        "source": path.name,
        "width": result.width,
        "height": result.height,
        "overview_width": result.overview_width,
        "overview_height": result.overview_height,
        "source_bytes": path.stat().st_size,
        "derived_bytes": _directory_size(output, limit=16 * 1024**3),
        "tile_count": result.tile_count,
        "first_tile_bands": first_tile_bands,
        "first_tile_has_alpha": first_tile_has_alpha,
        "wall_seconds": round(time.monotonic() - started, 3),
        "peak_rss_mib": round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1
        ),
        "current_rss_mib": _process_rss_mib(),
    }
    shutil.rmtree(output)
    return report


def _process_rss_mib(pid: int | None = None) -> float:
    status = Path(f"/proc/{pid or 'self'}/status").read_text()
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return round(int(line.split()[1]) / 1024, 1)
    return 0.0


def _run_cli(path: Path) -> dict:
    output = path.parent / f"{path.stem}-cli"
    command = [
        "vips",
        "dzsave",
        str(path),
        str(output),
        "--layout=dz",
        "--depth=onepixel",
        "--tile-size=512",
        "--overlap=1",
        "--suffix=.webp[Q=88,strip]",
    ]
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    peak_rss_mib = 0.0
    while process.poll() is None:
        try:
            peak_rss_mib = max(peak_rss_mib, _process_rss_mib(process.pid))
        except FileNotFoundError:
            pass
        time.sleep(0.01)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        raise RuntimeError((stderr or stdout).strip() or "vips dzsave failed")
    output_files = Path(str(output) + "_files")
    descriptor = Path(str(output) + ".dzi")
    return {
        "wall_seconds": round(time.monotonic() - started, 3),
        "peak_rss_mib": peak_rss_mib,
        "derived_bytes": _directory_size(output_files, limit=16 * 1024**3)
        + descriptor.stat().st_size,
    }


def _correctness_fixtures(root: Path) -> list[tuple[Path, int, int]]:
    fixtures: list[tuple[Path, int, int]] = []

    non_power = root / "non-power.png"
    Image.new("RGB", (1025, 513), (20, 80, 160)).save(non_power)
    fixtures.append((non_power, 1025, 513))

    exif_icc = root / "exif-icc.jpg"
    image = Image.new("RGB", (257, 513), (200, 40, 80))
    exif = image.getexif()
    exif[274] = 6
    image.save(
        exif_icc,
        quality=92,
        exif=exif,
        icc_profile=Path("/usr/share/color/icc/sRGB.icc").read_bytes(),
    )
    fixtures.append((exif_icc, 513, 257))

    alpha = root / "alpha.png"
    Image.new("RGBA", (513, 257), (40, 180, 90, 96)).save(alpha)
    fixtures.append((alpha, 513, 257))

    grayscale = root / "grayscale.png"
    Image.new("L", (513, 257), 127).save(grayscale)
    fixtures.append((grayscale, 513, 257))
    return fixtures


def _large_fixture(root: Path, case: str) -> tuple[Path, int, int]:
    width, height = LARGE_CASES[case]
    source = root / f"{case}.tif"
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import pyvips,sys;"
                "w=int(sys.argv[1]);h=int(sys.argv[2]);"
                "c=pyvips.Image.xyz(w,h);x=c.extract_band(0);y=c.extract_band(1);"
                "im=(x%256).bandjoin(y%256).bandjoin((x+y)%256).cast('uchar');"
                "im.write_to_file(sys.argv[3] + "
                "'[tile,tile-width=512,tile-height=512,compression=jpeg,Q=88,bigtiff]')"
            ),
            str(width),
            str(height),
            str(source),
        ],
        check=True,
        timeout=600,
    )
    return source, width, height


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case", choices=("correctness", *LARGE_CASES), default="correctness"
    )
    parser.add_argument("--compare-cli", action="store_true")
    parser.add_argument("--rounds", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 20:
        parser.error("--rounds must be between 1 and 20")
    with tempfile.TemporaryDirectory(prefix="aap-pyramid-benchmark-") as temp:
        root = Path(temp)
        if args.case == "correctness":
            results = [
                _run_fixture(path, width, height)
                for path, width, height in _correctness_fixtures(root)
            ]
            by_source = {row["source"]: row for row in results}
            if not by_source["alpha.png"]["first_tile_has_alpha"]:
                raise RuntimeError("alpha fixture lost its alpha channel")
            if by_source["grayscale.png"]["first_tile_bands"] != 3:
                raise RuntimeError("grayscale fixture was not normalized to sRGB")
            corrupt = root / "corrupt.jpg"
            corrupt.write_bytes(b"not-an-image")
            corrupt_error = None
            try:
                _run_fixture(corrupt, 10, 10)
            except ImageProcessingError as exc:
                corrupt_error = exc.code
            if corrupt_error != "decode_failed":
                raise RuntimeError(
                    f"corrupt fixture returned {corrupt_error!r}, expected decode_failed"
                )
            report = {"case": args.case, "fixtures": results, "corrupt": corrupt_error}
        else:
            fixture = _large_fixture(root, args.case)
            report = {
                "case": args.case,
                "fixtures": [_run_fixture(*fixture) for _ in range(args.rounds)],
            }
            if args.compare_cli:
                report["cli"] = _run_cli(fixture[0])
        print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
