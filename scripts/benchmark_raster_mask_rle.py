#!/usr/bin/env python3
"""Reproducible Phase A benchmark for raster-mask COCO RLE storage."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import platform
import subprocess
import tempfile
import time
from pathlib import Path
from statistics import median

import numpy as np
from PIL import Image
from mask_utils import encode_coco_rle


ROOT = Path(__file__).resolve().parents[1]
MAX_RUNS = 1_000_000
GEOMETRY_LIMIT = 8 * 1024 * 1024
STAGED_LIMIT = 64 * 1024 * 1024
RESOLUTIONS = {"720p": (720, 1280), "1080p": (1080, 1920), "4k": (2160, 3840)}


def compact_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def encode_numpy(mask: np.ndarray) -> list[int]:
    flat = np.asarray(mask, dtype=np.bool_).T.reshape(-1)
    changes = np.flatnonzero(flat[1:] != flat[:-1]) + 1
    bounds = np.concatenate(([0], changes, [flat.size]))
    counts = np.diff(bounds).astype(np.int64).tolist()
    if bool(flat[0]):
        counts.insert(0, 0)
    return counts


def decode_python(rle: dict) -> bytearray:
    height, width = rle["size"]
    out = bytearray(width * height)
    offset = 0
    foreground = False
    for run_length in rle["counts"]:
        if foreground:
            for index in range(offset, offset + run_length):
                x, y = divmod(index, height)
                out[y * width + x] = 1
        offset += run_length
        foreground = not foreground
    return out


def timed_decode(rle: dict, iterations: int = 5) -> tuple[float, float]:
    decode_python(rle)
    timings = []
    for _ in range(iterations):
        started = time.perf_counter()
        decode_python(rle)
        timings.append((time.perf_counter() - started) * 1000)
    timings.sort()
    return median(timings), timings[-1]


def resized_real_masks(masks_dir: Path, limit: int = 6) -> list[tuple[str, Image.Image]]:
    paths = sorted(masks_dir.glob("real_sam_*.png"))[:limit]
    if not paths:
        raise ValueError(f"No real_sam_*.png masks found in {masks_dir}")
    return [(path.stem, Image.open(path).convert("L")) for path in paths]


def synthetic_masks(height: int, width: int) -> list[tuple[str, np.ndarray]]:
    y, x = np.ogrid[:height, :width]
    rng = np.random.default_rng(220)
    return [
        (
            "rectangle",
            (x > width * 0.2)
            & (x < width * 0.8)
            & (y > height * 0.2)
            & (y < height * 0.8),
        ),
        (
            "ellipse",
            ((x - width / 2) / (width * 0.35)) ** 2
            + ((y - height / 2) / (height * 0.3)) ** 2
            < 1,
        ),
        ("checkerboard", (x + y) % 2 == 0),
        ("noise_50pct", rng.random((height, width)) >= 0.5),
    ]


def percentile(values: list[int], p: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def benchmark(masks_dir: Path) -> dict:
    samples: list[dict] = []
    node_samples: list[dict] = []
    real_sources = resized_real_masks(masks_dir)
    for resolution, (height, width) in RESOLUTIONS.items():
        masks: list[tuple[str, np.ndarray, str]] = []
        for name, image in real_sources:
            resized = image.resize((width, height), Image.Resampling.NEAREST)
            masks.append((name, np.asarray(resized) > 0, "real_sam"))
        masks.extend(
            (name, mask, "synthetic") for name, mask in synthetic_masks(height, width)
        )
        for name, mask, source in masks:
            counts = encode_numpy(mask)
            rle = {"encoding": "coco_rle", "size": [height, width], "counts": counts}
            raw = compact_bytes(rle)
            accepted = len(counts) <= MAX_RUNS
            if accepted:
                installed_rle = encode_coco_rle(mask.reshape(-1), width, height)
                if installed_rle != rle:
                    raise ValueError(f"Installed mask codec disagrees for {resolution}/{name}")
            p50 = p95 = None
            if accepted and (source == "real_sam" or name in {"rectangle", "ellipse"}):
                p50, p95 = timed_decode(rle)
            row = {
                "name": name,
                "source": source,
                "resolution": resolution,
                "height": height,
                "width": width,
                "runs": len(counts),
                "rle_json_bytes": len(raw),
                "gzip_bytes": len(gzip.compress(raw)),
                "accepted_by_run_limit": accepted,
                "python_decode_p50_ms": p50,
                "python_decode_p95_ms": p95,
                "row_major_buffer_bytes": height * width,
            }
            samples.append(row)
            if accepted and source == "real_sam" and resolution in {"1080p", "4k"}:
                node_samples.append(
                    {
                        "name": name,
                        "resolution": resolution,
                        "width": width,
                        "height": height,
                        "rle": rle,
                    }
                )

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump({"samples": node_samples}, handle, separators=(",", ":"))
        node_input = handle.name
    try:
        node_result = subprocess.run(
            [
                "node",
                "--experimental-strip-types",
                str(ROOT / "apps/web/scripts/benchmark-raster-mask-rle.mjs"),
                node_input,
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        node = json.loads(node_result.stdout)
    finally:
        os.unlink(node_input)

    real_1080 = [
        row
        for row in samples
        if row["source"] == "real_sam" and row["resolution"] == "1080p"
    ]
    p95_rle_bytes = percentile([row["rle_json_bytes"] for row in real_1080], 0.95)
    sample_rle = next(
        row for row in node_samples if len(compact_bytes(row["rle"])) == p95_rle_bytes
    )["rle"]
    keyframes = [
        {
            "frame_index": index,
            "mask": sample_rle,
            "source": "prediction",
            "occluded": False,
            "attributes": None,
        }
        for index in range(30)
    ]
    geometry_30 = {
        "type": "video_track_mask",
        "track_id": "benchmark",
        "semantic_label": None,
        "keyframes": keyframes,
        "outside": [],
    }
    staged_30x10 = {
        "output_geometry": "mask",
        "instances": [
            {"instance_id": str(index), "geometry": geometry_30} for index in range(10)
        ],
    }
    geometry_30_bytes = len(compact_bytes(geometry_30))
    staged_30x10_raw = compact_bytes(staged_30x10)
    per_keyframe = (
        geometry_30_bytes - len(compact_bytes({**geometry_30, "keyframes": []}))
    ) / 30
    projections = {
        "real_1080p_p95_rle_json_bytes": p95_rle_bytes,
        "actual_geometry_30x1_json_bytes": geometry_30_bytes,
        "actual_staged_30x10_json_bytes": len(staged_30x10_raw),
        "actual_staged_30x10_gzip_bytes": len(gzip.compress(staged_30x10_raw)),
        "projected_geometry_3000x1_json_bytes": round(per_keyframe * 3000),
        "projected_staged_300x10_json_bytes": round(per_keyframe * 300 * 10),
    }
    gate = {
        "inline_geometry_pass": projections["projected_geometry_3000x1_json_bytes"]
        <= GEOMETRY_LIMIT,
        "inline_staged_pass": projections["projected_staged_300x10_json_bytes"]
        <= STAGED_LIMIT,
    }
    node_1080 = [row["p95_ms"] for row in node["rows"] if row["resolution"] == "1080p"]
    node_4k = [row["p95_ms"] for row in node["rows"] if row["resolution"] == "4k"]
    gate["node_1080p_decode_pass"] = max(node_1080) <= 16
    gate["node_4k_decode_pass"] = max(node_4k) <= 50
    gate["decision"] = "inline" if all(gate.values()) else "coco_rle_ref"
    return {
        "metadata": {
            "git_sha": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "python": platform.python_version(),
            "node": node["node"],
            "os": platform.platform(),
            "cpu": platform.processor() or platform.machine(),
            "random_seed": 220,
            "python_iterations": 5,
            "node_iterations": 7,
            "byte_definition": "UTF-8 compact JSON with ensure_ascii=false and separators comma/colon",
        },
        "limits": {
            "max_runs": MAX_RUNS,
            "geometry_bytes": GEOMETRY_LIMIT,
            "staged_bytes": STAGED_LIMIT,
        },
        "samples": samples,
        "node_decode": node["rows"],
        "projections": projections,
        "gate": gate,
    }


def render_report(result: dict) -> str:
    projections = result["projections"]
    gate = result["gate"]
    real_rows = [row for row in result["samples"] if row["source"] == "real_sam"]
    lines = [
        "# Raster mask COCO RLE storage benchmark",
        "",
        "> Generated by `uv run --with-requirements ../_shared/requirements-plugin-mask.txt python ../../scripts/benchmark_raster_mask_rle.py --masks-dir <real-sam-masks>` from `apps/api`.",
        "",
        "## Outcome",
        "",
        f"**Decision: `{gate['decision']}`.** Inline storage gate: geometry={'pass' if gate['inline_geometry_pass'] else 'fail'}, "
        f"staged={'pass' if gate['inline_staged_pass'] else 'fail'}; Node decode: 1080p={'pass' if gate['node_1080p_decode_pass'] else 'fail'}, "
        f"4K={'pass' if gate['node_4k_decode_pass'] else 'fail'}.",
        "",
        "The storage decision is driven by uncompressed JSONB rewrite and staged-candidate size, not gzip transfer size. "
        "The implementation must use immutable content-addressed `coco_rle_ref` objects.",
        "",
        "## Gate measurements",
        "",
        "| Measurement | Bytes | Limit |",
        "|---|---:|---:|",
        f"| Real SAM 1080p p95 single RLE | {projections['real_1080p_p95_rle_json_bytes']:,} | 1,000,000 runs |",
        f"| Actual 30 frames x 1 track geometry | {projections['actual_geometry_30x1_json_bytes']:,} | 8 MiB |",
        f"| Projected 3000 frames x 1 track geometry | {projections['projected_geometry_3000x1_json_bytes']:,} | 8 MiB |",
        f"| Actual 30 frames x 10 targets staged JSON | {projections['actual_staged_30x10_json_bytes']:,} | 64 MiB |",
        f"| Actual 30 x 10 staged gzip | {projections['actual_staged_30x10_gzip_bytes']:,} | informational |",
        f"| Projected 300 frames x 10 targets staged JSON | {projections['projected_staged_300x10_json_bytes']:,} | 64 MiB |",
        "",
        "## Real SAM samples",
        "",
        "| Resolution | Sample | Runs | RLE JSON bytes | Gzip bytes | Python decode p95 ms |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in real_rows:
        lines.append(
            f"| {row['resolution']} | {row['name']} | {row['runs']:,} | {row['rle_json_bytes']:,} | "
            f"{row['gzip_bytes']:,} | {row['python_decode_p95_ms']:.2f} |"
        )
    lines.extend(
        [
            "",
            "## Node decode",
            "",
            "| Resolution | Sample | p50 ms | p95 ms |",
            "|---|---|---:|---:|",
        ]
    )
    for row in result["node_decode"]:
        lines.append(
            f"| {row['resolution']} | {row['name']} | {row['p50_ms']:.2f} | {row['p95_ms']:.2f} |"
        )
    lines.extend(
        [
            "",
            "## Reproduction metadata",
            "",
            "```json",
            json.dumps(result["metadata"], ensure_ascii=False, indent=2),
            "```",
            "",
            "Synthetic checkerboard and seeded 50% noise are retained in the raw JSON as adversarial cases. "
            "Combinations above 30 x 10 are projected rather than materialized to avoid benchmark-induced OOM.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--masks-dir", type=Path, required=True, help="Directory containing real_sam_*.png fixtures")
    parser.add_argument(
        "--json",
        type=Path,
        default=ROOT / "docs/research/data/16-raster-mask-rle-benchmark.json",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "docs/research/16-raster-mask-rle-benchmark.md",
    )
    args = parser.parse_args()
    result = benchmark(args.masks_dir)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    args.report.write_text(render_report(result))
    print(json.dumps(result["gate"], ensure_ascii=False))


if __name__ == "__main__":
    main()
