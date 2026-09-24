"""Evaluate pose-conditioned and metric DA3 on sampled nuScenes anchors, read-only.

The pose-conditioned candidate receives RGB, intrinsics and relative camera
poses; the metric candidate receives one RGB view and its intrinsics. LiDAR is
reserved for held-out scoring and a display-only surface rejection rule.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from build_mapanything_review import (
    CAMERA_ROLES,
    _depth_raster,
    _frame_source,
    _project_returns,
    _save_surface,
    _table,
    _three_coordinates,
)
from qualify_depth_reconstruction import _metrics, _nearest_lidar_errors, _scene_samples

MODEL_ID = "depth-anything/DA3-LARGE-1.1"
MODEL_REVISION = "0e109ae307c5982f319a67cf6f9f99ccdc0ec97c"
MODEL_CODE_REVISION = "3d835ec1a5802d64a8b8b15f817a1ab54809bfe4"
MODELS = {
    "da3-large-1.1": (MODEL_ID, MODEL_REVISION, False),
    "da3metric-large": (
        "depth-anything/DA3METRIC-LARGE",
        "4010e39f3634a45bc60553321fb49fb760bd594e",
        True,
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nuscenes-root", type=Path, required=True)
    parser.add_argument("--scene", default="scene-0061")
    parser.add_argument("--model", choices=MODELS, default="da3-large-1.1")
    parser.add_argument("--anchors", default="1,19,37")
    parser.add_argument("--cameras", default="all")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    model_id, model_revision, metric_mono = MODELS[args.model]

    import torch
    from depth_anything_3.api import DepthAnything3
    import depth_anything_3.api as da3_api

    if torch.cuda.device_count() != 1:
        raise RuntimeError("Expose exactly one GPU with CUDA_VISIBLE_DEVICES")
    model_source = Path(da3_api.__file__).resolve().parent.parent.parent
    revision = subprocess.check_output(
        ["git", "-C", str(model_source), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != MODEL_CODE_REVISION:
        raise RuntimeError(
            f"DA3 source must be {MODEL_CODE_REVISION}, found {revision}"
        )

    root = args.nuscenes_root.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    meta = root / "v1.0-mini"
    scene = next(
        (row for row in _table(meta, "scene").values() if row["name"] == args.scene),
        None,
    )
    if scene is None:
        raise ValueError(f"Scene {args.scene!r} not found")
    sequence = _scene_samples(scene, _table(meta, "sample"))
    sample_data = _table(meta, "sample_data")
    data_by_sample: dict[str, list[dict]] = {}
    for row in sample_data.values():
        data_by_sample.setdefault(row["sample_token"], []).append(row)
    calibrations = _table(meta, "calibrated_sensor")
    sensors = _table(meta, "sensor")
    poses = _table(meta, "ego_pose")
    anchors = [int(value) for value in args.anchors.split(",")]
    if (
        not anchors
        or len(set(anchors)) != len(anchors)
        or any(index < 0 or index >= len(sequence) for index in anchors)
    ):
        raise ValueError("Anchors must be distinct key-frame indices in the scene")
    roles = CAMERA_ROLES if args.cameras == "all" else tuple(args.cameras.split(","))
    if (
        not roles
        or len(set(roles)) != len(roles)
        or any(role not in CAMERA_ROLES for role in roles)
    ):
        raise ValueError("Invalid camera roles")

    def source(index: int, role: str) -> dict:
        return _frame_source(
            root,
            index,
            sequence[index],
            role,
            data_by_sample,
            calibrations,
            sensors,
            poses,
        )

    reference_from_global = np.linalg.inv(
        source(0, CAMERA_ROLES[0])["global_from_ego_lidar"]
    )
    load_start = time.perf_counter()
    model = (
        DepthAnything3.from_pretrained(model_id, revision=model_revision)
        .to("cuda")
        .eval()
    )
    load_s = round(time.perf_counter() - load_start, 2)
    torch.cuda.reset_peak_memory_stats()
    results = []
    for anchor in anchors:
        window = (
            [anchor]
            if metric_mono
            else list(range(max(0, anchor - 1), min(len(sequence), anchor + 2)))
        )
        anchor_local = window.index(anchor)
        for role in roles:
            group = [source(index, role) for index in window]
            world_from_global = np.linalg.inv(group[0]["global_from_camera"])
            extrinsics = np.stack(
                [
                    np.linalg.inv(world_from_global @ item["global_from_camera"])
                    for item in group
                ]
            ).astype(np.float32)
            intrinsics = np.stack([item["intrinsic"] for item in group]).astype(
                np.float32
            )
            started = time.perf_counter()
            prediction = model.inference(
                image=[item["image"] for item in group],
                extrinsics=None if metric_mono else extrinsics,
                intrinsics=intrinsics,
                align_to_input_ext_scale=not metric_mono,
                ref_view_strategy="middle",
                process_res=504,
                export_dir=None,
            )
            torch.cuda.synchronize()
            inference_s = round(time.perf_counter() - started, 2)
            item = group[anchor_local]
            depth = np.asarray(prediction.depth[anchor_local], dtype=np.float32)
            rgb = np.asarray(prediction.processed_images[anchor_local], dtype=np.uint8)
            if metric_mono:
                _, _, processed_intrinsics = model.input_processor(
                    [item["image"]], intrinsics=intrinsics, process_res=504
                )
                intrinsic = processed_intrinsics[0].numpy().astype(np.float64)
                focal = float((intrinsic[0, 0] + intrinsic[1, 1]) / 2)
                depth = depth * (focal / 300.0)
            else:
                intrinsic = np.asarray(
                    prediction.intrinsics[anchor_local], dtype=np.float64
                )
            sky = prediction.sky[anchor_local] if prediction.sky is not None else None
            mask = np.isfinite(depth) & (depth >= 1) & (depth <= 80)
            if sky is not None:
                mask &= ~sky
            height, width = depth.shape
            projected, visible = _project_returns(item, intrinsic, width, height)
            raster, holdout = _depth_raster(item, projected, visible, width, height)
            pixels = projected[holdout]
            truth = item["returns"][holdout, 2]
            prediction_at_holdout = depth[pixels[:, 1], pixels[:, 0]]
            valid = mask[pixels[:, 1], pixels[:, 0]]
            baseline = _nearest_lidar_errors(
                item["returns"][item["prompt"]], item["returns"][holdout]
            )
            reference_from_camera = reference_from_global @ item["global_from_camera"]
            orientation = reference_from_camera[:3, :3]
            camera_view = {
                "position": _three_coordinates(reference_from_camera[:3, 3]),
                "forward": _three_coordinates(orientation @ np.array([0, 0, 1])),
                "up": _three_coordinates(orientation @ np.array([0, -1, 0])),
            }
            prefix = f"frame-{anchor:02d}-{role.lower().replace('_', '-')}"
            rgb_file = f"{prefix}-rgb.jpg"
            depth_file = f"{prefix}-depth.jpg"
            Image.fromarray(rgb).save(output / rgb_file, quality=88)
            depth_color = cv2.applyColorMap(
                np.uint8(np.clip(depth, 0, 80) / 80 * 255), cv2.COLORMAP_TURBO
            )
            Image.fromarray(cv2.cvtColor(depth_color, cv2.COLOR_BGR2RGB)).save(
                output / depth_file, quality=87
            )
            # Sparse LiDAR rejects contradictory triangles in the visual-only
            # surface. It does not alter the unfiltered depth metric above.
            surface = _save_surface(
                output,
                prefix,
                depth,
                mask,
                raster,
                rgb,
                intrinsic,
                reference_from_camera,
            )
            result = {
                "frame_index": anchor,
                "input_frames": window,
                "camera_role": role,
                "size": [width, height],
                "inference_s": inference_s,
                "holdout_returns": int(holdout.sum()),
                "valid_holdout_returns": int(valid.sum()),
                "model_on_valid": _metrics(
                    np.abs(prediction_at_holdout[valid] - truth[valid])
                )
                if valid.any()
                else None,
                "lidar_on_valid": _metrics(baseline[valid]) if valid.any() else None,
                "lidar_all": _metrics(baseline),
                "valid_fraction": round(float(mask.mean()), 4),
                "sky_fraction": round(float(sky.mean()), 4)
                if sky is not None
                else None,
                "images": {"rgb": rgb_file, "depth": depth_file},
                "surface": surface,
                "camera_view": camera_view,
            }
            results.append(result)
            print(
                f"frame {anchor + 1}/{len(sequence)} {role}: {inference_s}s, MAE {result['model_on_valid']}",
                flush=True,
            )
    summary = {
        "scene": args.scene,
        "scene_sample_count": len(sequence),
        "anchors": anchors,
        "cameras": roles,
        "model_id": model_id,
        "model_revision": model_revision,
        "input_mode": "single_rgb_metric" if metric_mono else "three_rgb_pose",
        "model_code_revision": revision,
        "device": torch.cuda.get_device_name(0),
        "torch_version": torch.__version__,
        "load_s": load_s,
        "total_inference_s": round(sum(item["inference_s"] for item in results), 2),
        "peak_torch_reserved_mib": round(torch.cuda.max_memory_reserved() / 1048576, 1),
        "views": results,
    }
    (output / "data.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    project_root = Path(__file__).resolve().parents[3]
    ui_source = project_root / "apps/web/scripts/da3-review-ui"
    shutil.copy2(ui_source / "index.html", output / "index.html")
    esbuild = sorted(
        (project_root / "node_modules/.pnpm").glob(
            "esbuild@*/node_modules/esbuild/bin/esbuild"
        )
    )
    if not esbuild:
        raise RuntimeError("Repository esbuild dependency unavailable")
    subprocess.run(
        [
            str(esbuild[-1]),
            str(ui_source / "viewer.js"),
            "--bundle",
            "--format=esm",
            "--minify",
            f"--outfile={output / 'viewer.js'}",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
