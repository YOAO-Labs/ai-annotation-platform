"""Compare single-frame and temporal MapAnything artifacts on identical held-out returns."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def _array(
    directory: Path, name: str, dtype: str, shape: tuple[int, ...]
) -> np.ndarray:
    values = np.fromfile(directory / name, dtype=dtype)
    if values.size != int(np.prod(shape)):
        raise ValueError(f"unexpected file length: {directory / name}")
    return values.reshape(shape)


def _mae(
    depth: np.ndarray, pixels: np.ndarray, truth: np.ndarray, valid: np.ndarray
) -> float:
    y, x = pixels[:, 1], pixels[:, 0]
    return round(float(np.abs(depth[y[valid], x[valid]] - truth[valid]).mean()), 4)


def compare(single_dir: Path, temporal_dir: Path) -> dict:
    single = json.loads((single_dir / "data.json").read_text(encoding="utf-8"))
    temporal = json.loads((temporal_dir / "data.json").read_text(encoding="utf-8"))
    if single["scene"] != temporal["scene"] or single["cameras"] != temporal["cameras"]:
        raise ValueError("scene or camera roles differ")
    if single["model_revision"] != temporal["model_revision"]:
        raise ValueError("model revisions differ")
    if single["model_code_revision"] != temporal["model_code_revision"]:
        raise ValueError("model source revisions differ")
    by_role = {frame["camera_role"]: frame for frame in single["frames"]}
    results = []
    for current in temporal["frames"]:
        original = by_role[current["camera_role"]]
        if (
            original["frame_index"] != current["frame_index"]
            or original["size"] != current["size"]
        ):
            raise ValueError("anchor frame or processed size differs")
        width, height = current["size"]
        shape = (height, width)
        first_holdout = _array(
            single_dir,
            original["holdout_values"],
            "<f4",
            (original["holdout_returns"], 3),
        )
        second_holdout = _array(
            temporal_dir,
            current["holdout_values"],
            "<f4",
            (current["holdout_returns"], 3),
        )
        if not np.array_equal(first_holdout, second_holdout):
            raise ValueError(f"held-out returns differ for {current['camera_role']}")
        pixels = second_holdout[:, :2].astype(int)
        truth = second_holdout[:, 2]
        masks = {
            "single": _array(single_dir, original["mask_values"], "u1", shape).astype(
                bool
            ),
            "temporal_raw": _array(
                temporal_dir, current["mask_values"], "u1", shape
            ).astype(bool),
            "fused": _array(
                temporal_dir, current["temporal"]["mask_values"], "u1", shape
            ).astype(bool),
            "strict": _array(
                temporal_dir, current["temporal"]["strict_mask_values"], "u1", shape
            ).astype(bool),
        }
        depths = {
            "single": _array(single_dir, original["depth_values"], "<f4", shape),
            "temporal_raw": _array(temporal_dir, current["depth_values"], "<f4", shape),
            "fused": _array(
                temporal_dir, current["temporal"]["depth_values"], "<f4", shape
            ),
        }
        depths["strict"] = depths["fused"]
        valid = {
            name: mask[pixels[:, 1], pixels[:, 0]]
            & np.isfinite(depths[name][pixels[:, 1], pixels[:, 0]])
            & (depths[name][pixels[:, 1], pixels[:, 0]] > 0)
            for name, mask in masks.items()
        }
        common = valid["single"] & valid["temporal_raw"]
        added = valid["fused"] & ~valid["temporal_raw"]
        if not common.any():
            raise ValueError(f"no common held-out returns for {current['camera_role']}")
        results.append(
            {
                "camera_role": current["camera_role"],
                "single_coverage": round(float(masks["single"].mean()), 4),
                "temporal_raw_coverage": round(float(masks["temporal_raw"].mean()), 4),
                "fused_coverage": round(float(masks["fused"].mean()), 4),
                "strict_coverage": round(float(masks["strict"].mean()), 4),
                "filled_fraction": current["temporal"]["filled_fraction"],
                "common_holdout_count": int(common.sum()),
                "single_mae_on_common": _mae(depths["single"], pixels, truth, common),
                "temporal_raw_mae_on_common": _mae(
                    depths["temporal_raw"], pixels, truth, common
                ),
                "fused_mae_on_common": _mae(depths["fused"], pixels, truth, common),
                "new_holdout_count": int(added.sum()),
                "new_holdout_mae": _mae(depths["fused"], pixels, truth, added)
                if added.any()
                else None,
                "strict_holdout_count": int(valid["strict"].sum()),
                "strict_mae_on_valid": _mae(
                    depths["strict"], pixels, truth, valid["strict"]
                )
                if valid["strict"].any()
                else None,
            }
        )
    return {
        "scene": single["scene"],
        "anchor_frame": temporal["frames"][0]["frame_index"],
        "single_inference_s": single["inference_s"],
        "temporal_inference_s": temporal["inference_s"],
        "cameras": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("single_dir", type=Path)
    parser.add_argument("temporal_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(args.single_dir, args.temporal_dir), indent=2))


if __name__ == "__main__":
    main()
