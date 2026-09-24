"""Build a resumable, read-only six-camera review of every nuScenes scene key frame.

Run in an isolated MapAnything/CUDA environment. The output is a local static
review artifact and never becomes task geometry or annotation data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np

from build_mapanything_review import (
    CAMERA_ROLES,
    MODEL_CODE_REVISION,
    MODEL_ID,
    MODEL_REVISION,
    _extract_result,
    _frame_source,
    _points_to_reference,
    _prepare_views,
    _save_cloud,
    _table,
    _temporal_view,
)
from mapanything_scene import assemble_scene_mesh
from mapanything_temporal import fuse_anchor, temporal_support
from qualify_depth_reconstruction import _scene_samples


def _fingerprint(scene: dict, sequence: list[dict]) -> str:
    digest = hashlib.sha256()
    for filename in (
        "build_mapanything_scene_review.py",
        "build_mapanything_review.py",
        "mapanything_scene.py",
        "mapanything_temporal.py",
    ):
        digest.update((Path(__file__).parent / filename).read_bytes())
    digest.update(MODEL_REVISION.encode())
    digest.update(scene["token"].encode())
    for sample in sequence:
        digest.update(sample["token"].encode())
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def _scene_lidar(
    output: Path, sequence: list[dict], load_source, reference_from_global: np.ndarray
) -> dict:
    cells: dict[tuple[int, int, int], np.ndarray] = {}
    for frame_index in range(len(sequence)):
        source = load_source(frame_index, CAMERA_ROLES[0])
        transform = (
            reference_from_global
            @ source["global_from_ego_lidar"]
            @ source["ego_from_lidar"]
        )
        valid = np.isfinite(source["lidar_points"]).all(axis=1) & (
            np.linalg.norm(source["lidar_points"], axis=1) <= 80
        )
        points = _points_to_reference(source["lidar_points"], transform)
        for point, cell in zip(
            points[valid], np.floor(points[valid] / 0.2).astype(int), strict=True
        ):
            cells.setdefault(tuple(cell), point)
    xyz = np.asarray(list(cells.values()), dtype=np.float32)
    rgb = np.tile(np.array([[69, 205, 235]], dtype=np.uint8), (len(xyz), 1))
    filename = "scene-lidar.bin"
    _save_cloud(output / filename, xyz, rgb)
    return {"file": filename, "point_count": len(xyz), "voxel_m": 0.2}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nuscenes-root", type=Path, required=True)
    parser.add_argument("--scene", default="scene-0061")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--stop-after", type=int, default=0, help="Only for a resumable smoke run"
    )
    args = parser.parse_args()
    if args.stop_after < 0:
        parser.error("--stop-after must be nonnegative")

    import mapanything
    import torch
    from mapanything.models import MapAnything

    if torch.cuda.device_count() != 1:
        raise RuntimeError("Expose exactly one GPU with CUDA_VISIBLE_DEVICES")
    model_source = Path(mapanything.__file__).resolve().parent.parent
    model_code_revision = subprocess.check_output(
        ["git", "-C", str(model_source), "rev-parse", "HEAD"], text=True
    ).strip()
    if model_code_revision != MODEL_CODE_REVISION:
        raise RuntimeError(f"MapAnything source must be {MODEL_CODE_REVISION}")

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
    fingerprint = _fingerprint(scene, sequence)
    manifest_path = output / "manifest.json"
    manifest = {
        "scene": args.scene,
        "sample_count": len(sequence),
        "cameras": list(CAMERA_ROLES),
        "fingerprint": fingerprint,
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "model_code_revision": model_code_revision,
    }
    if manifest_path.exists():
        if not args.resume or json.loads(manifest_path.read_text()) != manifest:
            raise ValueError(
                "Existing output requires --resume and an identical scene/source fingerprint"
            )
    elif any(output.iterdir()):
        raise ValueError("Output directory is not empty and has no matching manifest")
    else:
        _atomic_json(manifest_path, manifest)

    cache: dict[tuple[int, str], dict] = {}

    def load_source(frame_index: int, role: str) -> dict:
        key = (frame_index, role)
        if key not in cache:
            cache[key] = _frame_source(
                root,
                frame_index,
                sequence[frame_index],
                role,
                data_by_sample,
                calibrations,
                sensors,
                poses,
            )
        return cache[key]

    first_source = load_source(0, CAMERA_ROLES[0])
    reference_from_global = np.linalg.inv(first_source["global_from_ego_lidar"])
    completed = []
    for index in range(len(sequence)):
        checkpoint = output / f"frame-{index:02d}.json"
        if checkpoint.exists():
            completed.append(json.loads(checkpoint.read_text(encoding="utf-8")))
    if len(completed) < len(sequence):
        model = (
            MapAnything.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
            .to("cuda")
            .eval()
        )
        torch.cuda.reset_peak_memory_stats()
        produced = 0
        for frame_index, sample in enumerate(sequence):
            checkpoint = output / f"frame-{frame_index:02d}.json"
            if checkpoint.exists():
                continue
            start = time.perf_counter()
            window = list(
                range(max(0, frame_index - 1), min(len(sequence), frame_index + 2))
            )
            anchor_index = window.index(frame_index)
            views = []
            inference_s = 0.0
            for role in CAMERA_ROLES:
                group = [load_source(index, role) for index in window]
                prepared = _prepare_views(
                    group, np.linalg.inv(group[0]["global_from_camera"])
                )
                infer_start = time.perf_counter()
                with torch.inference_mode():
                    predictions = model.infer(
                        prepared,
                        memory_efficient_inference=True,
                        minibatch_size=1,
                        use_amp=True,
                        amp_dtype="bf16",
                        apply_mask=True,
                        mask_edges=True,
                        apply_confidence_mask=False,
                    )
                torch.cuda.synchronize()
                inference_s += time.perf_counter() - infer_start
                predictions = [
                    {
                        key: item[key].cpu()
                        for key in ("depth_z", "conf", "mask", "img_no_norm")
                    }
                    for item in predictions
                ]
                temporal_views = [
                    _temporal_view(source, processed, prediction)
                    for source, processed, prediction in zip(
                        group, prepared, predictions, strict=True
                    )
                ]
                supported, cross_frame = temporal_support(temporal_views)
                depth, mask, rgb, filled = fuse_anchor(
                    temporal_views, supported, anchor_index
                )
                views.append(
                    _extract_result(
                        output,
                        group[anchor_index],
                        prepared[anchor_index],
                        predictions[anchor_index],
                        reference_from_global,
                        {
                            "depth": depth,
                            "mask": mask,
                            "rgb": rgb,
                            "filled": filled,
                            "supported": supported[anchor_index],
                            "cross_frame": cross_frame[anchor_index],
                        },
                    )
                )
                del predictions, prepared, temporal_views
            result = {
                "frame_index": frame_index,
                "sample_token": sample["token"],
                "timestamp": sample["timestamp"],
                "input_frames": window,
                "views": views,
                "inference_s": round(inference_s, 2),
                "generation_s": round(time.perf_counter() - start, 2),
            }
            _atomic_json(checkpoint, result)
            completed.append(result)
            produced += 1
            cache.clear()
            print(
                f"frame {frame_index + 1}/{len(sequence)}: inference {inference_s:.1f}s, total {result['generation_s']:.1f}s",
                flush=True,
            )
            if args.stop_after and produced >= args.stop_after:
                return
        peak_reserved_mib = round(torch.cuda.max_memory_reserved() / 1048576, 1)
        del model
    else:
        peak_reserved_mib = None

    completed.sort(key=lambda item: item["frame_index"])
    if [row["frame_index"] for row in completed] != list(range(len(sequence))):
        raise ValueError("Scene checkpoints are incomplete")
    mesh = {}
    for mode, key in (("fused", "surface"), ("strict", "strict_surface")):
        sources = [
            (view["temporal"][key], view["camera_view"]["position"])
            for frame in completed
            for view in frame["views"]
        ]
        mesh[mode] = assemble_scene_mesh(output, f"scene-{mode}", sources)
    lidar = _scene_lidar(output, sequence, load_source, reference_from_global)
    summary = {
        **manifest,
        "device": torch.cuda.get_device_name(0),
        "torch_version": torch.__version__,
        "peak_torch_reserved_mib": peak_reserved_mib,
        "total_inference_s": round(sum(frame["inference_s"] for frame in completed), 2),
        "frames": completed,
        "scene_mesh": mesh,
        "scene_lidar": lidar,
    }
    _atomic_json(output / "data.json", summary)
    project_root = Path(__file__).resolve().parents[3]
    ui_source = project_root / "apps/web/scripts/mapanything-scene-review-ui"
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
    print(
        json.dumps(
            {
                "scene": args.scene,
                "frames": len(completed),
                "views": sum(len(f["views"]) for f in completed),
                "mesh": mesh,
                "lidar": lidar,
            },
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
