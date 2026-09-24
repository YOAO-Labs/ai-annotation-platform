"""Build a read-only, local MapAnything review artifact from nuScenes mini.

Requires MapAnything, PyTorch and its dependencies in an isolated environment.
The output is a static visual experiment; it is never attached to a Task or
published as annotation data. Serve the output directory over localhost to
open its index.html in a browser.
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
from PIL import Image, ImageDraw

from import_nuscenes_scene import (
    _compute_lidar_to_cam_extrinsic,
    _load_table,
    _resolve_source_path,
    _transform,
)
from qualify_depth_reconstruction import (
    _metrics,
    _nearest_lidar_errors,
    _prompt_split,
    _scene_samples,
    _visible_lidar_returns,
)

MODEL_ID = "facebook/map-anything-apache"
MODEL_REVISION = "00f9c245bbcb60522d1ed7f9e9d88462c6e3f38a"
MODEL_CODE_REVISION = "3d10cf7a3016fc0f9bb13a071ee66c47b10be0d9"
CAMERA_ROLES = (
    "CAM_FRONT",
    "CAM_FRONT_RIGHT",
    "CAM_BACK_RIGHT",
    "CAM_BACK",
    "CAM_BACK_LEFT",
    "CAM_FRONT_LEFT",
)


def _table(meta: Path, name: str) -> dict[str, dict]:
    return {row["token"]: row for row in _load_table(meta, name)}


def _channel(row: dict, calibrations: dict[str, dict], sensors: dict[str, dict]) -> str:
    return sensors[calibrations[row["calibrated_sensor_token"]]["sensor_token"]][
        "channel"
    ]


def _frame_source(
    root: Path,
    frame_index: int,
    sample: dict,
    camera_role: str,
    data_by_sample: dict[str, list[dict]],
    calibrations: dict[str, dict],
    sensors: dict[str, dict],
    poses: dict[str, dict],
) -> dict:
    frame_data = {
        _channel(row, calibrations, sensors): row
        for row in data_by_sample[sample["token"]]
        if row["is_key_frame"]
    }
    lidar = frame_data["LIDAR_TOP"]
    camera = frame_data[camera_role]
    lidar_calibration = calibrations[lidar["calibrated_sensor_token"]]
    camera_calibration = calibrations[camera["calibrated_sensor_token"]]
    lidar_pose = poses[lidar["ego_pose_token"]]
    camera_pose = poses[camera["ego_pose_token"]]
    lidar_points = np.fromfile(
        _resolve_source_path(root, lidar["filename"]), dtype="<f4"
    ).reshape(-1, 5)[:, :3]
    with Image.open(_resolve_source_path(root, camera["filename"])) as source_image:
        image = source_image.convert("RGB")
    intrinsic = np.asarray(camera_calibration["camera_intrinsic"], dtype=np.float64)
    extrinsic = np.asarray(
        _compute_lidar_to_cam_extrinsic(
            cs_lidar=lidar_calibration,
            ego_lidar=lidar_pose,
            cs_cam=camera_calibration,
            ego_cam=camera_pose,
        ),
        dtype=np.float64,
    ).reshape(4, 4)
    returns = _visible_lidar_returns(
        lidar_points, extrinsic, intrinsic, image.width, image.height
    )
    prompt, holdout, _ = _prompt_split(returns, image.width, image.height)
    return {
        "frame_index": frame_index,
        "camera_role": camera_role,
        "image": image,
        "intrinsic": intrinsic,
        "extrinsic": extrinsic,
        "lidar_points": lidar_points,
        "returns": returns,
        "prompt": prompt,
        "holdout": holdout,
        "ego_from_lidar": _transform(
            lidar_calibration["rotation"], lidar_calibration["translation"]
        ),
        "global_from_ego_lidar": _transform(
            lidar_pose["rotation"], lidar_pose["translation"]
        ),
        "global_from_camera": _transform(
            camera_pose["rotation"], camera_pose["translation"]
        )
        @ _transform(camera_calibration["rotation"], camera_calibration["translation"]),
        "lidar_camera_delta_us": camera["timestamp"] - lidar["timestamp"],
    }


def _project_returns(
    source: dict, processed_intrinsic: np.ndarray, width: int, height: int
) -> tuple[np.ndarray, np.ndarray]:
    returns = source["returns"]
    transform = processed_intrinsic @ np.linalg.inv(source["intrinsic"])
    image_coords = (
        np.stack((returns[:, 0], returns[:, 1], np.ones(len(returns))), axis=1)
        @ transform.T
    )
    x = np.floor(image_coords[:, 0] / image_coords[:, 2]).astype(int)
    y = np.floor(image_coords[:, 1] / image_coords[:, 2]).astype(int)
    visible = (x >= 0) & (x < width) & (y >= 0) & (y < height)
    return np.stack((x, y), axis=1), visible


def _depth_raster(
    source: dict, projected: np.ndarray, visible: np.ndarray, width: int, height: int
) -> tuple[np.ndarray, np.ndarray]:
    prompt = source["prompt"] & visible
    x, y = projected[:, 0], projected[:, 1]
    raster = np.full((height, width), np.inf, dtype=np.float32)
    np.minimum.at(raster, (y[prompt], x[prompt]), source["returns"][prompt, 2])
    raster[~np.isfinite(raster)] = 0
    holdout = source["holdout"] & visible
    holdout &= raster[np.clip(y, 0, height - 1), np.clip(x, 0, width - 1)] == 0
    return raster, holdout


def _points_to_reference(points: np.ndarray, transform: np.ndarray) -> np.ndarray:
    return points @ transform[:3, :3].T + transform[:3, 3]


def _three_coordinates(vector: np.ndarray) -> list[float]:
    return [float(vector[1]), float(vector[2]), -float(vector[0])]


def _save_cloud(path: Path, xyz: np.ndarray, rgb: np.ndarray) -> int:
    count = min(len(xyz), len(rgb))
    record = np.empty(
        count,
        dtype=[
            ("x", "<f4"),
            ("y", "<f4"),
            ("z", "<f4"),
            ("r", "u1"),
            ("g", "u1"),
            ("b", "u1"),
            ("padding", "u1"),
        ],
    )
    record["x"] = xyz[:count, 1]  # ISO left -> Three right
    record["y"] = xyz[:count, 2]  # ISO up -> Three up
    record["z"] = -xyz[:count, 0]  # ISO forward -> Three forward
    record["r"] = rgb[:count, 0]
    record["g"] = rgb[:count, 1]
    record["b"] = rgb[:count, 2]
    record["padding"] = 0
    record.tofile(path)
    return count


def _mesh_faces(depth: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Join nearby valid depth samples without spanning large depth jumps."""
    height, width = depth.shape
    indices = np.arange(height * width, dtype=np.uint32).reshape(height, width)
    a, b = indices[:-1, :-1], indices[:-1, 1:]
    c, d = indices[1:, :-1], indices[1:, 1:]

    def accepted(i: np.ndarray, j: np.ndarray, k: np.ndarray) -> np.ndarray:
        flat_depth = depth.ravel()
        near = np.minimum(np.minimum(flat_depth[i], flat_depth[j]), flat_depth[k])
        far = np.maximum(np.maximum(flat_depth[i], flat_depth[j]), flat_depth[k])
        return (
            valid.ravel()[i]
            & valid.ravel()[j]
            & valid.ravel()[k]
            & (far - near <= 0.25 + 0.04 * near)
        )

    first = np.stack((a, b, c), axis=-1)[accepted(a, b, c)]
    second = np.stack((b, d, c), axis=-1)[accepted(b, d, c)]
    return np.concatenate((first, second), axis=0).astype("<u4", copy=False)


def _save_surface(
    output: Path,
    prefix: str,
    depth: np.ndarray,
    mask: np.ndarray,
    raster: np.ndarray,
    rgb: np.ndarray,
    intrinsic: np.ndarray,
    reference_from_camera: np.ndarray,
) -> dict:
    sample_y, sample_x = np.mgrid[0 : depth.shape[0] : 2, 0 : depth.shape[1] : 2]
    z = depth[sample_y, sample_x]
    prompt_depth = raster[sample_y, sample_x]
    valid = (
        mask[sample_y, sample_x]
        & np.isfinite(z)
        & (z >= 1)
        & (z <= 80)
        & (
            (prompt_depth == 0)
            | (np.abs(z - prompt_depth) <= np.maximum(1, 0.1 * prompt_depth))
        )
    )
    camera_xyz = np.stack(
        (
            (sample_x - intrinsic[0, 2]) / intrinsic[0, 0] * z,
            (sample_y - intrinsic[1, 2]) / intrinsic[1, 1] * z,
            z,
        ),
        axis=-1,
    )
    camera_xyz[~valid] = 0
    vertices = _points_to_reference(camera_xyz.reshape(-1, 3), reference_from_camera)
    vertex_file = f"{prefix}-surface-vertices.bin"
    index_file = f"{prefix}-surface-indices.bin"
    preview_index_file = f"{prefix}-surface-preview-indices.bin"
    vertex_count = _save_cloud(
        output / vertex_file, vertices, rgb[sample_y, sample_x].reshape(-1, 3)
    )
    faces = _mesh_faces(z, valid)
    faces.tofile(output / index_file)
    preview_grid_indices = np.arange(z.size, dtype="<u4").reshape(z.shape)[::3, ::3]
    preview_faces = _mesh_faces(z[::3, ::3], valid[::3, ::3])
    preview_indices = preview_grid_indices.ravel()[preview_faces]
    preview_indices.tofile(output / preview_index_file)
    return {
        "vertices": vertex_file,
        "indices": index_file,
        "preview_indices": preview_index_file,
        "vertex_count": vertex_count,
        "triangle_count": int(len(faces)),
        "preview_triangle_count": int(len(preview_faces)),
        "valid_vertex_fraction": round(float(valid.mean()), 4),
    }


def _save_image_overlays(
    output: Path,
    prefix: str,
    rgb: np.ndarray,
    depth: np.ndarray,
    confidence: np.ndarray,
    projected: np.ndarray,
    source: dict,
    holdout: np.ndarray,
) -> dict[str, str]:
    Image.fromarray(rgb).save(output / f"{prefix}-rgb.jpg", quality=88)
    depth_color = cv2.applyColorMap(
        np.uint8(np.clip(depth, 0, 80) / 80 * 255), cv2.COLORMAP_TURBO
    )
    Image.fromarray(cv2.cvtColor(depth_color, cv2.COLOR_BGR2RGB)).save(
        output / f"{prefix}-depth.jpg", quality=87
    )
    finite_confidence = confidence[np.isfinite(confidence)]
    low, high = np.quantile(finite_confidence, [0.02, 0.98])
    normalized = np.clip((confidence - low) / max(high - low, 1e-6), 0, 1)
    Image.fromarray(np.uint8(normalized * 255)).save(
        output / f"{prefix}-confidence.png"
    )

    prompt_overlay = Image.new("RGBA", (rgb.shape[1], rgb.shape[0]))
    error_overlay = Image.new("RGBA", (rgb.shape[1], rgb.shape[0]))
    prompt_draw = ImageDraw.Draw(prompt_overlay)
    error_draw = ImageDraw.Draw(error_overlay)
    for x, y in projected[source["prompt"]]:
        if 0 <= x < rgb.shape[1] and 0 <= y < rgb.shape[0]:
            prompt_draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill=(38, 248, 168, 230))
    for point_index in np.flatnonzero(holdout):
        x, y = projected[point_index]
        residual = abs(float(depth[y, x]) - float(source["returns"][point_index, 2]))
        color = (51, 224, 138, 230) if residual < 1 else (255, 185, 50, 240)
        if residual >= 3:
            color = (255, 72, 80, 240)
        error_draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=color)
    prompt_overlay.save(output / f"{prefix}-prompt.png")
    error_overlay.save(output / f"{prefix}-error.png")
    return {
        "rgb": f"{prefix}-rgb.jpg",
        "depth": f"{prefix}-depth.jpg",
        "confidence": f"{prefix}-confidence.png",
        "prompt": f"{prefix}-prompt.png",
        "error": f"{prefix}-error.png",
    }


def _extract_result(
    output: Path,
    source: dict,
    processed_view: dict,
    prediction: dict,
    reference_from_global: np.ndarray,
) -> dict:
    frame_index = source["frame_index"]
    camera_role = source["camera_role"]
    depth = prediction["depth_z"][0, :, :, 0].float().cpu().numpy()
    confidence = prediction["conf"][0].float().cpu().numpy()
    mask = prediction["mask"][0, :, :, 0].bool().cpu().numpy()
    rgb = np.uint8(
        np.clip(prediction["img_no_norm"][0].float().cpu().numpy() * 255, 0, 255)
    )
    height, width = depth.shape
    intrinsic = processed_view["intrinsics"][0].cpu().numpy()
    projected, visible = _project_returns(source, intrinsic, width, height)
    raster, holdout = _depth_raster(source, projected, visible, width, height)
    prefix = f"frame-{frame_index:02d}-{camera_role.lower().replace('_', '-')}"
    np.asarray(depth, dtype="<f4").tofile(output / f"{prefix}-depth.bin")
    np.asarray(raster, dtype="<f4").tofile(output / f"{prefix}-lidar-depth.bin")
    truth = source["returns"][holdout, 2]
    pixels = projected[holdout]
    predicted = depth[pixels[:, 1], pixels[:, 0]]
    errors = np.abs(predicted - truth)
    baseline = _nearest_lidar_errors(
        source["returns"][source["prompt"]], source["returns"][holdout]
    )
    valid_holdout = (
        mask[pixels[:, 1], pixels[:, 0]] & np.isfinite(predicted) & (predicted > 0)
    )
    images = _save_image_overlays(
        output, prefix, rgb, depth, confidence, projected, source, holdout
    )

    ego_from_lidar = source["ego_from_lidar"]
    reference_from_lidar = (
        reference_from_global @ source["global_from_ego_lidar"] @ ego_from_lidar
    )
    camera_points = _points_to_reference(source["lidar_points"], source["extrinsic"])
    projected_lidar = camera_points @ source["intrinsic"].T
    with np.errstate(divide="ignore", invalid="ignore"):
        image_x = projected_lidar[:, 0] / projected_lidar[:, 2]
        image_y = projected_lidar[:, 1] / projected_lidar[:, 2]
    within_camera = (
        np.isfinite(image_x)
        & np.isfinite(image_y)
        & (camera_points[:, 2] >= 1)
        & (camera_points[:, 2] <= 80)
        & (image_x >= 0)
        & (image_x < source["image"].width)
        & (image_y >= 0)
        & (image_y < source["image"].height)
    )
    lidar_points = source["lidar_points"][within_camera]
    lidar_xyz = _points_to_reference(lidar_points, reference_from_lidar)
    lidar_rgb = np.tile(np.array([[69, 205, 235]], dtype=np.uint8), (len(lidar_xyz), 1))
    lidar_count = _save_cloud(output / f"{prefix}-lidar.bin", lidar_xyz, lidar_rgb)

    sample_y, sample_x = np.mgrid[0:height:3, 0:width:3]
    z = depth[sample_y, sample_x]
    valid = (z >= 1) & (z <= 80) & np.isfinite(z) & mask[sample_y, sample_x]
    x = sample_x[valid]
    y = sample_y[valid]
    z = z[valid]
    camera_xyz = np.stack(
        (
            (x - intrinsic[0, 2]) / intrinsic[0, 0] * z,
            (y - intrinsic[1, 2]) / intrinsic[1, 1] * z,
            z,
        ),
        axis=1,
    )
    reference_from_camera = reference_from_lidar @ np.linalg.inv(source["extrinsic"])
    camera_orientation = reference_from_camera[:3, :3]
    camera_view = {
        "position": _three_coordinates(reference_from_camera[:3, 3]),
        "forward": _three_coordinates(camera_orientation @ np.array([0, 0, 1])),
        "up": _three_coordinates(camera_orientation @ np.array([0, -1, 0])),
    }
    surface = _save_surface(
        output, prefix, depth, mask, raster, rgb, intrinsic, reference_from_camera
    )
    model_xyz = _points_to_reference(camera_xyz, reference_from_camera)
    model_rgb = rgb[y, x]
    model_confidence = confidence[y, x]
    confident_cutoff = float(np.median(model_confidence))
    model_count = _save_cloud(output / f"{prefix}-model.bin", model_xyz, model_rgb)
    confident = model_confidence >= confident_cutoff
    confident_count = _save_cloud(
        output / f"{prefix}-confident.bin", model_xyz[confident], model_rgb[confident]
    )
    return {
        "frame_index": frame_index,
        "camera_role": camera_role,
        "size": [width, height],
        "prompt_returns": int(source["prompt"].sum()),
        "prompt_cells": int(np.count_nonzero(raster)),
        "holdout_returns": int(holdout.sum()),
        "model": _metrics(errors),
        "lidar_nearest": _metrics(baseline),
        "valid_holdout_returns": int(valid_holdout.sum()),
        "model_on_valid": _metrics(errors[valid_holdout])
        if valid_holdout.any()
        else None,
        "lidar_on_valid": _metrics(baseline[valid_holdout])
        if valid_holdout.any()
        else None,
        "model_mask_fraction": round(float(mask.mean()), 4),
        "confident_threshold": round(confident_cutoff, 5),
        "lidar_camera_delta_us": source["lidar_camera_delta_us"],
        "cloud_counts": {
            "lidar": lidar_count,
            "model": model_count,
            "confident": confident_count,
        },
        "images": images,
        "clouds": {
            "lidar": f"{prefix}-lidar.bin",
            "model": f"{prefix}-model.bin",
            "confident": f"{prefix}-confident.bin",
        },
        "surface": surface,
        "camera_view": camera_view,
        "depth_values": f"{prefix}-depth.bin",
        "lidar_depth_values": f"{prefix}-lidar-depth.bin",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nuscenes-root", type=Path, required=True)
    parser.add_argument("--scene", default="scene-0061")
    parser.add_argument("--frames", default="0,1")
    parser.add_argument("--camera", default="CAM_FRONT")
    parser.add_argument(
        "--cameras", help="Comma-separated camera roles, or 'all' for six cameras"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    import mapanything
    import torch
    from mapanything.models import MapAnything
    from mapanything.utils.image import preprocess_inputs

    if torch.cuda.device_count() != 1:
        raise RuntimeError("Expose exactly one GPU with CUDA_VISIBLE_DEVICES")
    model_source = Path(mapanything.__file__).resolve().parent.parent
    try:
        model_code_revision = subprocess.check_output(
            ["git", "-C", str(model_source), "rev-parse", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(
            "Install MapAnything from the pinned source checkout"
        ) from error
    if model_code_revision != MODEL_CODE_REVISION:
        raise RuntimeError(
            f"MapAnything source revision {model_code_revision} does not match "
            f"{MODEL_CODE_REVISION}"
        )
    root = args.nuscenes_root.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    meta = root / "v1.0-mini"
    scenes = _table(meta, "scene")
    samples = _table(meta, "sample")
    sample_data = _table(meta, "sample_data")
    calibrations = _table(meta, "calibrated_sensor")
    sensors = _table(meta, "sensor")
    poses = _table(meta, "ego_pose")
    scene = next((row for row in scenes.values() if row["name"] == args.scene), None)
    if scene is None:
        raise ValueError(f"scene {args.scene!r} not found")
    sequence = _scene_samples(scene, samples)
    data_by_sample: dict[str, list[dict]] = {}
    for row in sample_data.values():
        data_by_sample.setdefault(row["sample_token"], []).append(row)
    selected = [int(index) for index in args.frames.split(",")]
    if args.cameras == "all":
        camera_roles = CAMERA_ROLES
    elif args.cameras:
        camera_roles = tuple(role.strip() for role in args.cameras.split(","))
    else:
        camera_roles = (args.camera,)
    if not camera_roles or any(role not in CAMERA_ROLES for role in camera_roles):
        raise ValueError(f"camera roles must be chosen from {CAMERA_ROLES}")
    if len(set(camera_roles)) != len(camera_roles):
        raise ValueError("camera roles must be unique")
    if not selected or len(selected) > 4 or len(selected) * len(camera_roles) > 8:
        raise ValueError("select 1 to 4 frames and at most 8 total views")
    if any(index < 0 or index >= len(sequence) for index in selected):
        raise ValueError("frame index outside the scene")
    sources = [
        _frame_source(
            root,
            index,
            sequence[index],
            role,
            data_by_sample,
            calibrations,
            sensors,
            poses,
        )
        for index in selected
        for role in camera_roles
    ]
    reference_from_global = np.linalg.inv(sources[0]["global_from_ego_lidar"])
    reference_camera_from_global = np.linalg.inv(sources[0]["global_from_camera"])
    input_views = [
        {
            "img": source["image"],
            "intrinsics": source["intrinsic"].astype(np.float32),
            "depth_z": np.zeros(
                (source["image"].height, source["image"].width), dtype=np.float32
            ),
            "camera_poses": (
                reference_camera_from_global @ source["global_from_camera"]
            ).astype(np.float32),
            "is_metric_scale": torch.tensor([True]),
        }
        for source in sources
    ]
    processed = preprocess_inputs(input_views)
    for source, view in zip(sources, processed, strict=True):
        height, width = view["depth_z"].shape[1:]
        coordinates, visible = _project_returns(
            source, view["intrinsics"][0].numpy(), width, height
        )
        raster, _ = _depth_raster(source, coordinates, visible, width, height)
        view["depth_z"] = torch.from_numpy(raster)[None]

    start_load = time.perf_counter()
    model = MapAnything.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
    model = model.to("cuda").eval()
    load_s = round(time.perf_counter() - start_load, 2)
    torch.cuda.reset_peak_memory_stats()
    start_infer = time.perf_counter()
    with torch.inference_mode():
        predictions = model.infer(
            processed,
            memory_efficient_inference=True,
            minibatch_size=1,
            use_amp=True,
            amp_dtype="bf16",
            apply_mask=True,
            mask_edges=True,
            apply_confidence_mask=False,
        )
    torch.cuda.synchronize()
    inference_s = round(time.perf_counter() - start_infer, 2)
    peak_reserved_mib = round(torch.cuda.max_memory_reserved() / 1048576, 1)
    results = [
        _extract_result(output, source, view, prediction, reference_from_global)
        for source, view, prediction in zip(
            sources, processed, predictions, strict=True
        )
    ]
    try:
        app_revision = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        app_revision = None
    summary = {
        "scene": args.scene,
        "camera": camera_roles[0] if len(camera_roles) == 1 else "MULTI_CAMERA",
        "cameras": camera_roles,
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "model_code_revision": model_code_revision,
        "app_revision": app_revision,
        "device": torch.cuda.get_device_name(0),
        "torch_version": torch.__version__,
        "load_s": load_s,
        "inference_s": inference_s,
        "peak_torch_reserved_mib": peak_reserved_mib,
        "frames": results,
    }
    (output / "data.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    project_root = Path(__file__).resolve().parents[3]
    ui_source = project_root / "apps/web/scripts/mapanything-review-ui"
    shutil.copy2(ui_source / "index.html", output / "index.html")
    esbuild_candidates = sorted(
        (project_root / "node_modules/.pnpm").glob(
            "esbuild@*/node_modules/esbuild/bin/esbuild"
        )
    )
    if not esbuild_candidates:
        raise RuntimeError(
            "esbuild is unavailable; install the repository Node dependencies"
        )
    subprocess.run(
        [
            str(esbuild_candidates[-1]),
            str(ui_source / "viewer.js"),
            "--bundle",
            "--format=esm",
            "--minify",
            f"--outfile={output / 'viewer.js'}",
        ],
        check=True,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
