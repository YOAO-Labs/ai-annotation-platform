"""Run the fixed, private VGGT-Omega / LiDAR densification experiment.

No application database, annotation task or production model is modified.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

from import_nuscenes_scene import _load_table, _resolve_source_path, _transform
from omega_densification import acquisition_split, resized_intrinsic, transform
from vggt_common import MemoryMonitor, _scene_samples, interpolate_box, write_json

RUNTIME_SOURCE_FILES = (
    "qualify_vggt_omega.py",
    "omega_densification.py",
    "vggt_common.py",
    "build_vggt_omega_review.py",
    "omega_textured_mesh.py",
    "build_vggt_omega_mesh.py",
    "audit_vggt_omega_artifacts.py",
    "audit_vggt_omega_mesh.py",
    "serve_vggt_review.py",
    "import_nuscenes_scene.py",
)

SOURCE_COMMIT = "48b23c8ce72c9a8fcf987f0d7f43d8e4870759f6"
WEIGHT_REVISION = "1041e80fc0e911235d3426b0a3d9a81075111a53"
WEIGHT_SHA = "e900833330eb0e570e10543e6108d0dd704fa7c8dbc32738a645987875298896"
CAMERAS = (
    "CAM_FRONT",
    "CAM_FRONT_RIGHT",
    "CAM_BACK_RIGHT",
    "CAM_BACK",
    "CAM_BACK_LEFT",
    "CAM_FRONT_LEFT",
)
PARAMETERS = {
    "resolution": 416,
    "mode": "balanced",
    "processed_size": [560, 320],
    "pixel_centres": "array integer; K uses PIL half-pixel resize affine",
    "voxel_m": 0.1,
    "confidence_quantile": 0.2,
    "local_neighbors": 8,
    "local_radius_px": 24,
    "local_rgb_l2_gate": 0.2,
    "local_depth_gate_m": 1,
    "local_depth_gate_relative": 0.15,
    "local_log_bound": float(np.log(1.3)),
    "minimum_local_neighbors": 2,
    "actor_padding_m": 0.2,
    "support_absolute_m": 0.4,
    "support_relative": 0.03,
    "support_cycle_px": 2,
    "minimum_temporal_support": 1,
    "maximum_visible_conflicts": 0,
    "anchor_absolute_m": 0.3,
    "anchor_relative": 0.03,
    "evaluation_pixel_radius": 1.5,
    "evaluation_tolerances_m": [0.2, 0.5, 1.0],
    "valid_depth_m": [1, 80],
    "ego_bottom_fraction": 0.05,
    "first_exposure": "centre CAM_FRONT for both window sizes",
    "dynamic_fusion": "centre RGB exposures only; no motion compensation",
    "static_mask": "oracle all vehicle/human/movable_object actor boxes at each exposure",
}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class SceneData:
    def __init__(self, root):
        self.root = root
        meta = root / "v1.0-mini"
        self.tables = {
            name: {row["token"]: row for row in _load_table(meta, name)}
            for name in (
                "scene",
                "sample",
                "sample_data",
                "calibrated_sensor",
                "sensor",
                "ego_pose",
                "sample_annotation",
                "instance",
                "category",
            )
        }
        self.frames = {}
        self.tracks = {}
        self.hashes = {}
        by_sample = defaultdict(dict)
        for row in self.tables["sample_data"].values():
            if row["is_key_frame"]:
                channel = self.tables["sensor"][
                    self.tables["calibrated_sensor"][row["calibrated_sensor_token"]][
                        "sensor_token"
                    ]
                ]["channel"]
                by_sample[row["sample_token"]][channel] = row
        for scene in self.tables["scene"].values():
            samples = _scene_samples(scene, self.tables["sample"])
            self.frames[scene["name"]] = [
                by_sample[sample["token"]] for sample in samples
            ]
            tokens = {sample["token"] for sample in samples}
            tracks = defaultdict(list)
            for annotation in self.tables["sample_annotation"].values():
                if annotation["sample_token"] not in tokens:
                    continue
                instance = self.tables["instance"][annotation["instance_token"]]
                category = self.tables["category"][instance["category_token"]]["name"]
                if category.startswith(("vehicle.", "human.", "movable_object.")):
                    tracks[annotation["instance_token"]].append(
                        {
                            **annotation,
                            "category": category,
                            "timestamp": self.tables["sample"][
                                annotation["sample_token"]
                            ]["timestamp"],
                        }
                    )
            self.tracks[scene["name"]] = [
                sorted(track, key=lambda b: b["timestamp"]) for track in tracks.values()
            ]

    def pose(self, row):
        cs = self.tables["calibrated_sensor"][row["calibrated_sensor_token"]]
        ego = self.tables["ego_pose"][row["ego_pose_token"]]
        return _transform(ego["rotation"], ego["translation"]) @ _transform(
            cs["rotation"], cs["translation"]
        )

    def boxes(self, scene, timestamp):
        # Only tracks present near this exposure; endpoints are conservatively clamped.
        return [
            interpolate_box(track, timestamp)
            for track in self.tracks[scene]
            if track[0]["timestamp"] - 100000
            <= timestamp
            <= track[-1]["timestamp"] + 100000
        ]

    def record(self, row):
        path = _resolve_source_path(self.root, row["filename"])
        if str(path) not in self.hashes:
            self.hashes[str(path)] = digest(path)
        return {
            "token": row["token"],
            "timestamp": row["timestamp"],
            "path": str(path),
            "sha256": self.hashes[str(path)],
            "global_from_sensor": self.pose(row).tolist(),
        }

    def scan(self, scene, frame):
        row = self.frames[scene][frame]["LIDAR_TOP"]
        xyz = np.fromfile(
            _resolve_source_path(self.root, row["filename"]), dtype="<f4"
        ).reshape(-1, 5)[:, :3]
        return transform(xyz, self.pose(row)), row


def freeze(args):
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    if (output / "protocol.json").exists():
        raise ValueError(
            "protocol already frozen; use infer/compare on the saved protocol"
        )
    data = SceneData(args.data)
    runs = []
    for scene in ("scene-0061", "scene-0103"):
        for centre in (8, 19, 30):
            for window in (3, 5):
                frames = list(range(centre - window // 2, centre + window // 2 + 1))
                views, scans = [], []
                for frame in frames:
                    lidar = data.frames[scene][frame]["LIDAR_TOP"]
                    scans.append({"frame": frame, **data.record(lidar)})
                    for camera in CAMERAS:
                        row = data.frames[scene][frame][camera]
                        cs = data.tables["calibrated_sensor"][
                            row["calibrated_sensor_token"]
                        ]
                        k = resized_intrinsic(
                            np.asarray(cs["camera_intrinsic"]), (1600, 900), (560, 320)
                        )
                        views.append(
                            {
                                "frame": frame,
                                "camera": camera,
                                "intrinsic": k.tolist(),
                                **data.record(row),
                            }
                        )
                views.sort(
                    key=lambda v: (
                        0 if v["frame"] == centre and v["camera"] == "CAM_FRONT" else 1,
                        v["timestamp"],
                        CAMERAS.index(v["camera"]),
                    )
                )
                target = next(s for s in scans if s["frame"] == centre)
                anchor_tokens = acquisition_split(
                    [s["token"] for s in scans], target["token"]
                )
                runs.append(
                    {
                        "id": f"{scene}-c{centre:02d}-w{window}",
                        "scene": scene,
                        "split": "development"
                        if scene == "scene-0061"
                        else "validation",
                        "centre": centre,
                        "window": window,
                        "frames": frames,
                        "views": views,
                        "scans": scans,
                        "target_scan": target["token"],
                        "anchor_scans": anchor_tokens,
                        "single_scan": next(
                            s["token"] for s in scans if s["frame"] == centre - 1
                        ),
                        "reference_from_global": np.linalg.inv(
                            np.asarray(target["global_from_sensor"])
                        ).tolist(),
                    }
                )
    write_json(
        output / "protocol.json",
        {
            "version": 1,
            "frozen_before_inference": True,
            "parameters": PARAMETERS,
            "source_commit": SOURCE_COMMIT,
            "weight_revision": WEIGHT_REVISION,
            "weight_sha256": WEIGHT_SHA,
            "data_root": args.data.resolve(),
            "task": "current-frame RGB-assisted densification; centre LiDAR entirely held out",
            "oracle_masks": True,
            "runs": runs,
            "source_hashes": {
                name: digest(Path(__file__).with_name(name))
                for name in RUNTIME_SOURCE_FILES
            },
        },
    )
    print(
        f"Frozen {len(runs)} windows; {len(data.hashes)} distinct sensor files",
        flush=True,
    )


def infer(args):
    if os.environ.get("CUDA_VISIBLE_DEVICES") != "1":
        raise ValueError("this pilot requires physical GPU 1 only")
    protocol = json.loads((args.output / "protocol.json").read_text())
    if digest(args.weights) != WEIGHT_SHA:
        raise ValueError("checkpoint SHA256 mismatch")
    commit = subprocess.check_output(
        ["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(args.source), "status", "--porcelain"], text=True
    ).strip()
    if commit != SOURCE_COMMIT or dirty:
        raise ValueError("expected the clean, pinned upstream source")
    sys.path.insert(0, str(args.source))
    import torch
    from vggt_omega.models import VGGTOmega
    from vggt_omega.utils.load_fn import load_and_preprocess_images

    torch.set_num_threads(8)
    torch.manual_seed(42)
    np.random.seed(42)
    output = args.output
    (output / "predictions").mkdir(exist_ok=True)
    verification = args.verify_existing
    suffix = "-verification" if verification else ""
    stage = ""
    if verification and (output / "verification-source.json").exists():
        stage = "-consolidated"
        suffix = f"{stage}-verification"
        if (output / f"verification-source{stage}.json").exists():
            raise ValueError(
                "consolidated verification already exists; preserve the previous proof"
            )
    if verification:
        snapshot = output / f"source-snapshot{stage}"
        snapshot.mkdir(exist_ok=True)
        source_hashes = {}
        for name in RUNTIME_SOURCE_FILES:
            original = Path(__file__).with_name(name)
            (snapshot / name).write_bytes(original.read_bytes())
            source_hashes[name] = digest(original)
        write_json(
            output / f"verification-source{stage}.json",
            {
                "source_hashes": source_hashes,
                "reason": "archive final executable source and verify unchanged saved predictions after wrapper formatting and postprocess audit fixes",
                "protocol_sha256": digest(output / "protocol.json"),
            },
        )
        initial = json.loads((output / "inference.json").read_text())
        expected = {r["id"]: r["prediction_sha256"] for r in initial["runs"]}
    records = []
    with MemoryMonitor(1) as monitor:
        t = time.perf_counter()
        model = VGGTOmega().eval()
        state = torch.load(args.weights, map_location="cpu", weights_only=True)
        model.load_state_dict(state, strict=True)
        del state
        model = model.to("cuda")
        torch.cuda.synchronize()
        print(f"Loaded official model in {time.perf_counter() - t:.1f}s", flush=True)
        for run in protocol["runs"]:
            destination = output / "predictions" / f"{run['id']}.npz"
            if destination.exists() and not verification:
                raise ValueError("refusing to silently reuse a previous prediction")
            if verification and digest(destination) != expected[run["id"]]:
                raise ValueError("saved prediction changed before verification")
            for view in run["views"]:
                if digest(view["path"]) != view["sha256"]:
                    raise ValueError("RGB input changed")
                with Image.open(view["path"]) as image:
                    if image.size != (1600, 900):
                        raise ValueError(
                            "pilot resize affine does not cover this source shape"
                        )
            images = load_and_preprocess_images(
                [v["path"] for v in run["views"]], mode="balanced", image_resolution=416
            )
            if tuple(images.shape) != (len(run["views"]), 3, 320, 560):
                raise ValueError(f"unexpected image grid: {tuple(images.shape)}")
            images = images.cuda()
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()
            started = time.perf_counter()
            try:
                with torch.inference_mode():
                    result = model(images)
                torch.cuda.synchronize()
                seconds = time.perf_counter() - started
                depth = result["depth"][0].float().cpu().numpy().squeeze(-1)
                confidence = result["depth_conf"][0].float().cpu().numpy()
                pose = result["pose_enc"]
                if isinstance(pose, (list, tuple)):
                    pose = pose[-1]
                pose = pose.float().cpu().numpy()
                rgb = np.uint8(
                    np.clip(images.cpu().permute(0, 2, 3, 1).numpy() * 255, 0, 255)
                )
                if (
                    depth.shape != (len(run["views"]), 320, 560)
                    or confidence.shape != depth.shape
                ):
                    raise ValueError(
                        f"unexpected depth/confidence grid: {depth.shape}, {confidence.shape}"
                    )
                if not np.isfinite(depth).all() or not np.isfinite(confidence).all():
                    raise ValueError("nonfinite predictions")
                differences = {}
                if verification:
                    with np.load(destination) as saved:
                        for name, array in {
                            "depth": depth,
                            "confidence": confidence,
                            "pose_enc": pose,
                            "rgb": rgb,
                        }.items():
                            differences[name] = {
                                "exact": bool(np.array_equal(saved[name], array)),
                                "max_abs": float(
                                    np.max(
                                        np.abs(
                                            saved[name].astype(float)
                                            - array.astype(float)
                                        )
                                    )
                                ),
                            }
                    if not all(row["exact"] for row in differences.values()):
                        raise ValueError(
                            f"prediction reproducibility mismatch: {differences}"
                        )
                else:
                    np.savez_compressed(
                        destination,
                        depth=depth,
                        confidence=confidence,
                        pose_enc=pose,
                        rgb=rgb,
                    )
                record = {
                    "id": run["id"],
                    "status": "completed",
                    "seconds": seconds,
                    "images": len(run["views"]),
                    "grid": [560, 320],
                    "peak_allocated_mib": torch.cuda.max_memory_allocated() / 1048576,
                    "peak_reserved_mib": torch.cuda.max_memory_reserved() / 1048576,
                    "prediction_sha256": digest(destination),
                    "depth_range": [float(depth.min()), float(depth.max())],
                }
                if verification:
                    record["array_verification"] = differences
                print(json.dumps(record), flush=True)
                del result, depth, confidence, pose, rgb
            except torch.cuda.OutOfMemoryError as error:
                record = {
                    "id": run["id"],
                    "status": "oom",
                    "message": str(error),
                    "seconds": time.perf_counter() - started,
                }
                print(f"OOM {run['id']}", flush=True)
            del images
            torch.cuda.empty_cache()
            records.append(record)
            write_json(
                output / f"inference{suffix}.json",
                {"runs": records, "source_commit": commit, "weight_sha256": WEIGHT_SHA},
            )
    write_json(output / f"resource-monitor{suffix}.json", monitor.summary())
    packages = {
        dist.metadata["Name"]: dist.version
        for dist in importlib.metadata.distributions()
    }
    write_json(
        output / f"environment{suffix}.json",
        {
            "python": sys.version,
            "packages": packages,
            "torch_cuda": torch.version.cuda,
            "visible_devices": os.environ["CUDA_VISIBLE_DEVICES"],
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("freeze", "infer", "compare"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--data",
        type=Path,
        default=Path.home() / ".cache/ai-annotation-platform/nuscenes-mini/content",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path.home() / ".x-repo/github.com/facebookresearch/vggt-omega",
    )
    parser.add_argument("--weights", type=Path)
    parser.add_argument("--verify-existing", action="store_true")
    args = parser.parse_args()
    if args.command == "freeze":
        freeze(args)
    elif args.command == "infer":
        infer(args)
    else:
        from build_vggt_omega_review import compare

        compare(args)


if __name__ == "__main__":
    main()
