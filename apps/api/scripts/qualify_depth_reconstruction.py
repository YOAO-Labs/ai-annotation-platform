"""Evaluate pretrained depth models against held-out nuScenes LiDAR returns.

This is an offline, read-only qualification tool. It does not create annotation
data or publish render assets. Install torch and transformers in an isolated
environment before running it; see the model-rendering plan for the command.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image

from import_nuscenes_scene import (
    _compute_lidar_to_cam_extrinsic,
    _load_table,
    _resolve_source_path,
)

MODEL_IDS = {
    "promptda-small": "depth-anything/prompt-depth-anything-vits-hf",
    "outdoor-small": "depth-anything/Depth-Anything-V2-Metric-Outdoor-Small-hf",
}
MODEL_REVISIONS = {
    "promptda-small": "6f3768a5383d95a904ec00d2a7ca3bd186f8797e",
    "outdoor-small": "fd2c22027eaf20374204f14099b8341e1925ad39",
}
PROMPT_HEIGHT = 192
PROMPT_WIDTH = 256


def _table(meta_dir: Path, name: str) -> dict[str, dict]:
    return {row["token"]: row for row in _load_table(meta_dir, name)}


def _scene_samples(scene: dict, samples: dict[str, dict]) -> list[dict]:
    result = []
    token = scene["first_sample_token"]
    seen = set()
    while token:
        if token in seen:
            raise ValueError("sample chain contains a cycle")
        seen.add(token)
        sample = samples[token]
        if sample["scene_token"] != scene["token"]:
            raise ValueError("sample chain crossed the scene boundary")
        result.append(sample)
        token = sample["next"]
    return result


def _visible_lidar_returns(
    points: np.ndarray,
    extrinsic: np.ndarray,
    intrinsic: np.ndarray,
    width: int,
    height: int,
) -> np.ndarray:
    camera = points @ extrinsic[:3, :3].T + extrinsic[:3, 3]
    projected = camera @ intrinsic.T
    depth = camera[:, 2]
    denominator = projected[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        u = projected[:, 0] / denominator
        v = projected[:, 1] / denominator
    valid = (
        np.isfinite(u)
        & np.isfinite(v)
        & (depth >= 1)
        & (depth <= 80)
        & (denominator > 0)
        & (u >= 0)
        & (u < width)
        & (v >= 0)
        & (v < height)
    )
    returns = np.stack((u[valid], v[valid], depth[valid]), axis=1)
    # Keep the nearest LiDAR return per original image pixel before splitting.
    pixel_id = np.floor(returns[:, 1]).astype(np.int64) * width + np.floor(
        returns[:, 0]
    ).astype(np.int64)
    order = np.lexsort((returns[:, 2], pixel_id))
    _, first = np.unique(pixel_id[order], return_index=True)
    return returns[order[first]]


def _prompt_split(
    returns: np.ndarray, width: int, height: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    pixels = np.floor(returns[:, :2]).astype(np.int64)
    # Split by original pixel, never by point array order.
    key = (pixels[:, 0] * 73856093) ^ (pixels[:, 1] * 19349663)
    prompt = key % 5 != 0
    holdout = ~prompt
    if prompt.sum() < 2 or holdout.sum() < 2:
        raise ValueError("too few visible LiDAR returns for a held-out test")

    x = np.clip((returns[:, 0] / width * PROMPT_WIDTH).astype(int), 0, PROMPT_WIDTH - 1)
    y = np.clip(
        (returns[:, 1] / height * PROMPT_HEIGHT).astype(int), 0, PROMPT_HEIGHT - 1
    )
    raster = np.full((PROMPT_HEIGHT, PROMPT_WIDTH), np.inf, dtype=np.float32)
    np.minimum.at(raster, (y[prompt], x[prompt]), returns[prompt, 2])
    raster[~np.isfinite(raster)] = 0

    # Exclude any held-out pixel that shares a downsampled prompt cell.
    holdout &= raster[y, x] == 0
    if holdout.sum() < 2:
        raise ValueError("too few independent held-out LiDAR returns")
    return prompt, holdout, raster


def _nearest_lidar_errors(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    errors = []
    for offset in range(0, len(target), 128):
        batch = target[offset : offset + 128]
        delta = batch[:, None, :2] - source[None, :, :2]
        nearest = np.argmin(np.sum(delta * delta, axis=2), axis=1)
        errors.append(np.abs(source[nearest, 2] - batch[:, 2]))
    return np.concatenate(errors)


def _sample_depth(depth: np.ndarray, returns: np.ndarray) -> np.ndarray:
    return depth[
        np.floor(returns[:, 1]).astype(int), np.floor(returns[:, 0]).astype(int)
    ]


def _metrics(errors: np.ndarray) -> dict[str, float]:
    return {
        "mae_m": round(float(np.mean(errors)), 3),
        "p90_ae_m": round(float(np.quantile(errors, 0.9)), 3),
    }


def _evaluate_frame_camera(
    *,
    root: Path,
    frame_index: int,
    role: str,
    frame_data: dict[str, dict],
    calibrations: dict[str, dict],
    poses: dict[str, dict],
    processor: object,
    model: object,
    device: str,
    use_prompt: bool,
) -> dict:
    import torch

    lidar = frame_data["LIDAR_TOP"]
    camera = frame_data[role]
    with Image.open(_resolve_source_path(root, camera["filename"])) as source_image:
        image = source_image.convert("RGB")
    width, height = image.size
    points = np.fromfile(
        _resolve_source_path(root, lidar["filename"]), dtype="<f4"
    ).reshape(-1, 5)[:, :3]
    extrinsic = np.asarray(
        _compute_lidar_to_cam_extrinsic(
            cs_lidar=calibrations[lidar["calibrated_sensor_token"]],
            ego_lidar=poses[lidar["ego_pose_token"]],
            cs_cam=calibrations[camera["calibrated_sensor_token"]],
            ego_cam=poses[camera["ego_pose_token"]],
        )
    ).reshape(4, 4)
    intrinsic = np.asarray(
        calibrations[camera["calibrated_sensor_token"]]["camera_intrinsic"]
    )
    returns = _visible_lidar_returns(points, extrinsic, intrinsic, width, height)
    prompt, holdout, raster = _prompt_split(returns, width, height)

    kwargs = {"images": image, "return_tensors": "pt"}
    if use_prompt:
        # The HF processor converts prompt values from millimetres to metres.
        kwargs["prompt_depth"] = Image.fromarray(raster * 1000)
    inputs = processor(**kwargs).to(device)
    if device.startswith("cuda"):
        torch.cuda.reset_peak_memory_stats()
    start = time.perf_counter()
    with torch.inference_mode():
        output = model(**inputs)
    if device.startswith("cuda"):
        torch.cuda.synchronize()
    inference_ms = round((time.perf_counter() - start) * 1000, 1)
    depth = (
        processor.post_process_depth_estimation(output, target_sizes=[(height, width)])[
            0
        ]["predicted_depth"]
        .float()
        .cpu()
        .numpy()
    )
    if not np.isfinite(depth).all():
        raise ValueError(
            f"model returned non-finite depth for frame {frame_index} {role}"
        )

    truth = returns[holdout, 2]
    predicted = _sample_depth(depth, returns[holdout])
    prompt_predicted = _sample_depth(depth, returns[prompt])
    # Affine fit is diagnostic only; fit on prompt points and score on holdout.
    scale, offset = np.linalg.lstsq(
        np.stack((prompt_predicted, np.ones_like(prompt_predicted)), axis=1),
        returns[prompt, 2],
        rcond=None,
    )[0]
    nearest = _nearest_lidar_errors(returns[prompt], returns[holdout])
    result = {
        "frame_index": frame_index,
        "camera": role,
        "image_size": [width, height],
        "lidar_camera_delta_us": camera["timestamp"] - lidar["timestamp"],
        "projected_returns": len(returns),
        "prompt_returns": int(prompt.sum()),
        "holdout_returns": int(holdout.sum()),
        "prompt_cells": int(np.count_nonzero(raster)),
        "inference_ms": inference_ms,
        "model": _metrics(np.abs(predicted - truth)),
        "lidar_nearest": _metrics(nearest),
        "model_affine_calibrated": _metrics(np.abs(scale * predicted + offset - truth)),
        "affine_scale_offset": [round(float(scale), 4), round(float(offset), 4)],
        "model_depth_range_m": [
            round(float(np.min(depth)), 3),
            round(float(np.max(depth)), 3),
        ],
        "negative_depth_fraction": round(float(np.mean(depth <= 0)), 5),
    }
    if device.startswith("cuda"):
        result["peak_inference_allocated_mib"] = round(
            torch.cuda.max_memory_allocated() / (1024 * 1024), 1
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nuscenes-root", type=Path, required=True)
    parser.add_argument("--scene", default="scene-0061")
    parser.add_argument("--frames", default="0,10,20,38")
    parser.add_argument(
        "--cameras",
        default="CAM_FRONT,CAM_FRONT_LEFT,CAM_FRONT_RIGHT,CAM_BACK,CAM_BACK_LEFT,CAM_BACK_RIGHT",
    )
    parser.add_argument("--model", choices=tuple(MODEL_IDS), required=True)
    parser.add_argument("--revision", help="override the pinned checkpoint revision")
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()

    import torch
    from transformers import AutoImageProcessor, AutoModelForDepthEstimation

    root = args.nuscenes_root.resolve(strict=True)
    meta = root / "v1.0-mini"
    scenes = _table(meta, "scene")
    samples = _table(meta, "sample")
    data = _table(meta, "sample_data")
    calibrations = _table(meta, "calibrated_sensor")
    poses = _table(meta, "ego_pose")
    sensors = _table(meta, "sensor")
    scene = next((row for row in scenes.values() if row["name"] == args.scene), None)
    if scene is None:
        raise ValueError(f"scene {args.scene!r} is absent")
    sequence = _scene_samples(scene, samples)
    frame_indices = [int(value) for value in args.frames.split(",")]
    roles = args.cameras.split(",")
    frame_map: dict[str, dict[str, dict]] = {}
    for row in data.values():
        if not row["is_key_frame"]:
            continue
        role = sensors[calibrations[row["calibrated_sensor_token"]]["sensor_token"]][
            "channel"
        ]
        frame_map.setdefault(row["sample_token"], {})[role] = row

    model_id = MODEL_IDS[args.model]
    revision = args.revision or MODEL_REVISIONS[args.model]
    processor = AutoImageProcessor.from_pretrained(model_id, revision=revision)
    model = (
        AutoModelForDepthEstimation.from_pretrained(model_id, revision=revision)
        .to(args.device)
        .eval()
    )
    rows = []
    for index in frame_indices:
        if index < 0 or index >= len(sequence):
            raise ValueError(
                f"frame index {index} outside scene with {len(sequence)} frames"
            )
        frame_data = frame_map[sequence[index]["token"]]
        for role in roles:
            if role not in frame_data:
                raise ValueError(f"{role} absent from frame {index}")
            rows.append(
                _evaluate_frame_camera(
                    root=root,
                    frame_index=index,
                    role=role,
                    frame_data=frame_data,
                    calibrations=calibrations,
                    poses=poses,
                    processor=processor,
                    model=model,
                    device=args.device,
                    use_prompt=args.model == "promptda-small",
                )
            )
    print(
        json.dumps(
            {
                "scene": args.scene,
                "scene_frames": len(sequence),
                "model_id": model_id,
                "model_revision": revision,
                "torch_version": torch.__version__,
                "device": args.device,
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
