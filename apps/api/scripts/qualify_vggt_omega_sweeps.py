"""Freeze all distinct scene exposures, then infer overlapping VGGT-Omega windows."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

from omega_densification import resized_intrinsic
from omega_dynamic import overlapping_windows
from qualify_vggt_omega import (
    CAMERAS,
    SOURCE_COMMIT,
    WEIGHT_REVISION,
    SceneData,
    digest,
)
from qualify_vggt_omega_sequence import HELDOUT, WEIGHT_SHA
from vggt_common import MemoryMonitor, write_json


def freeze(args):
    root = args.output
    root.mkdir(parents=True, exist_ok=True)
    destination = root / "sweeps-protocol.json"
    if destination.exists():
        raise ValueError("input protocol already frozen")
    data = SceneData(args.data)
    frames = data.frames[args.scene]
    sample_tokens = {f["LIDAR_TOP"]["sample_token"] for f in frames}
    selected = []
    for row in data.tables["sample_data"].values():
        if row["sample_token"] not in sample_tokens:
            continue
        cs = data.tables["calibrated_sensor"][row["calibrated_sensor_token"]]
        channel = data.tables["sensor"][cs["sensor_token"]]["channel"]
        if channel in (*CAMERAS, "LIDAR_TOP"):
            selected.append({**row, "channel": channel})
    selected.sort(key=lambda r: (r["timestamp"], r["channel"], r["token"]))
    overlay = root / "source-inputs"
    missing = {
        r["filename"] for r in selected if not (args.data / r["filename"]).is_file()
    }
    if missing:
        print(
            f"Extracting {len(missing)} missing sensor files from the local archive",
            flush=True,
        )
        with tarfile.open(args.archive, "r|gz") as archive:
            for member in archive:
                relative = member.name.removeprefix("./")
                if relative not in missing:
                    continue
                target = overlay / relative
                if not member.isfile() or not target.resolve().is_relative_to(
                    overlay.resolve()
                ):
                    raise ValueError("unsafe sensor archive member")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                missing.remove(relative)
                if not missing:
                    break
        if missing:
            raise ValueError(f"archive is missing {len(missing)} selected files")
    width, height = 640, 352
    key_index = {f["LIDAR_TOP"]["token"]: i for i, f in enumerate(frames)}
    hold_times = [frames[i]["LIDAR_TOP"]["timestamp"] for i in HELDOUT]
    views, scans = [], []
    for row in selected:
        path = args.data / row["filename"]
        if not path.is_file():
            path = overlay / row["filename"]
        record = {
            "token": row["token"],
            "sample_token": row["sample_token"],
            "timestamp": row["timestamp"],
            "filename": row["filename"],
            "path": str(path.resolve()),
            "sha256": digest(path),
            "global_from_sensor": data.pose(row),
            "is_key_frame": row["is_key_frame"],
        }
        if row["channel"] in CAMERAS:
            with Image.open(path) as image:
                if image.size != (1600, 900):
                    raise ValueError("unexpected source RGB shape")
            cs = data.tables["calibrated_sensor"][row["calibrated_sensor_token"]]
            views.append(
                {
                    **record,
                    "index": len(views),
                    "camera": row["channel"],
                    "intrinsic": resized_intrinsic(
                        np.asarray(cs["camera_intrinsic"]), (1600, 900), (width, height)
                    ),
                }
            )
        else:
            scans.append(
                {
                    **record,
                    "index": len(scans),
                    "key_frame": key_index.get(row["token"]),
                    "heldout": any(
                        abs(row["timestamp"] - t) <= 100000 for t in hold_times
                    ),
                }
            )
    if len(views) != 1318 or len(scans) != 382:
        raise ValueError("this frozen study expects exactly 1318 RGB and 382 scans")
    windows, owner = overlapping_windows(len(views))
    for view, own in zip(views, owner):
        view["owner_window"] = int(own)
    snapshot = root / "inference-source"
    snapshot.mkdir(exist_ok=True)
    sources = {}
    for name in (
        Path(__file__).name,
        "omega_dynamic.py",
        "qualify_vggt_omega.py",
        "qualify_vggt_omega_sequence.py",
        "vggt_common.py",
        "omega_densification.py",
        "import_nuscenes_scene.py",
    ):
        source = Path(__file__).with_name(name)
        shutil.copyfile(source, snapshot / name)
        sources[name] = digest(source)
    previous = args.previous
    previous_protocol = json.loads(
        (previous / "sequence-selected-protocol.json").read_text()
    )
    targets = []
    for frame in HELDOUT:
        path = previous / f"targets-{frame:02d}.npz"
        target = np.load(path)
        old_views = np.unique(target["view"]).astype(int)
        mapping = {
            int(i): next(
                v["index"]
                for v in views
                if v["token"] == previous_protocol["views"][i]["token"]
            )
            for i in old_views
        }
        targets.append(
            {
                "frame": frame,
                "path": str(path.resolve()),
                "sha256": digest(path),
                "targets": len(target["truth_z"]),
                "view_mapping": mapping,
            }
        )
    protocol = {
        "scene": args.scene,
        "data_root": args.data.resolve(),
        "archive": args.archive.resolve(),
        "scope": "all distinct RGB exposures associated with this scene's sample tokens; asynchronous exposures are not six-image synchronous frames",
        "input_images": len(views),
        "camera_counts": Counter(v["camera"] for v in views),
        "timestamp_span_s": (views[-1]["timestamp"] - views[0]["timestamp"]) / 1e6,
        "scans": scans,
        "views": views,
        "window_width": 144,
        "stride": 72,
        "windows": windows,
        "owner_rule": "greatest index distance from window edge; tie earlier window; one output per unique exposure",
        "source_commit": SOURCE_COMMIT,
        "weight_repo": "facebook/VGGT-Omega",
        "weight_file": "vggt_omega_1b_512.pt",
        "weight_revision": WEIGHT_REVISION,
        "weight_sha256": WEIGHT_SHA,
        "preprocess": {"resolution": 480, "mode": "balanced", "size": [width, height]},
        "inference": "one joint forward per overlapping window; BF16; Flash or efficient SDPA only; physical GPU1",
        "heldout_key_frames": HELDOUT,
        "heldout_exclusion_us": 100000,
        "heldout_tokens": [s["token"] for s in scans if s["heldout"]],
        "fixed_static_targets": targets,
        "calibration": {
            "branches": ["key", "all"],
            "maximum_scan_distance_us": 1000000,
            "maximum_scans_per_view": 4,
            "global_scale": "one robust metric depth scale per owner window and branch, unique raw scan-row identities",
            "local": "8NN/24px RGB .2 depth max(1m,.15Z); two neighbors, bounded log(1.3)",
        },
        "static": {
            "actor_padding_m": 0.3,
            "confidence_quantile": 0.2,
            "range_m": [1, 80],
            "voxel_m": 0.1,
            "cross_view": "adjacent actual exposures in same camera; nearest exposure in each neighboring camera within 125ms; different source IDs",
            "accept": "support>=1 or direct LiDAR; zero visible model conflict and no measured free-space conflict",
            "target_footprint_px": 1.5,
        },
        "dynamic": {
            "neighbourhood_us": 100000,
            "annotation_gap_us": 750000,
            "voxel_m": 0.05,
            "diagnostic_instances": [
                "c1958768d48640948f6053d04cffd35b",
                "bc38961ca0ac4b14ab90e547ba79fbb6",
                "e59d97d4403a48b88fbb6a58fc9959e6",
            ],
            "diagnostic_selection": "GT only before inference: persistent moving car visible in all three holdouts, fastest car visible in at least two holdouts, fastest pedestrian visible in all three; >=5 annotations, >=2s, median horizontal speed>=.5m/s; FOV is not proof of no occlusion",
            "actor_anchor_distance_us": 150000,
            "actor_scale_minimum_returns": 8,
            "actor_scale_bound": [0.5, 2.0],
            "anchor_gate_m": 0.4,
            "anchor_gate_relative": 0.04,
            "modes": ["single", "unaligned", "compensated"],
            "oracle": "known instance annotations; bracketed rigid poses without extrapolation or size normalization; people and articulated vehicles single exposure",
            "selection": "same pre-filter source IDs for rigid unaligned/compensated; nearest source timestamp then source ID; no RGB or geometry averaging",
            "visibility": "target-clock nearest CAM_FRONT; common point depth versus calibrated target depth tolerance max(.5m,.05Z); no target-actor box filter after compensation",
            "evaluation": "unique heldout raw surface returns per instance at actual scan time; nearest-cloud 3D distance is a sparse support proxy, not dense accuracy",
        },
        "metadata_sha256": {
            p.name: digest(p) for p in (args.data / "v1.0-mini").glob("*.json")
        },
        "source_hashes": sources,
    }
    write_json(destination, protocol)
    print(
        f"Frozen {len(views)} exposures, {len(windows)} windows, {len(scans)} scans, {len(protocol['heldout_tokens'])} excluded",
        flush=True,
    )


def infer(args):
    if os.environ.get("CUDA_VISIBLE_DEVICES") != "1":
        raise ValueError("only physical GPU1 is authorized")
    root = args.output
    path = root / "sweeps-protocol.json"
    protocol = json.loads(path.read_text())
    if (root / "sweeps-inference.json").exists():
        raise ValueError("preserve previous inference")
    if digest(args.weights) != protocol["weight_sha256"]:
        raise ValueError("checkpoint hash mismatch")
    commit = subprocess.check_output(
        ["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(args.source), "status", "--porcelain"], text=True
    ).strip()
    if commit != SOURCE_COMMIT or dirty:
        raise ValueError("upstream checkout is not clean and pinned")
    for name, expected in protocol["source_hashes"].items():
        if digest(Path(__file__).with_name(name)) != expected:
            raise ValueError(f"inference source changed: {name}")
    for view in protocol["views"]:
        if digest(view["path"]) != view["sha256"]:
            raise ValueError("input RGB changed")
    sys.path.insert(0, str(args.source))
    import torch
    from torch.nn.attention import SDPBackend, sdpa_kernel
    from vggt_omega.models import VGGTOmega
    from vggt_omega.utils.load_fn import load_and_preprocess_images
    from vggt_omega.utils.pose_enc import encoding_to_camera

    torch.set_num_threads(8)
    torch.manual_seed(42)
    np.random.seed(42)
    destination = root / "window-predictions"
    destination.mkdir()
    record = {
        "protocol_sha256": digest(path),
        "physical_gpu": 1,
        "input_images": 1318,
        "windows": [],
        "forward_calls": 0,
        "status": "running",
    }
    with MemoryMonitor(1) as monitor:
        model = VGGTOmega().eval()
        model.load_state_dict(
            torch.load(args.weights, map_location="cpu", weights_only=True), strict=True
        )
        model = model.cuda()
        print("Loaded pinned VGGT-Omega; starting overlapping windows", flush=True)
        try:
            for index, indices in enumerate(protocol["windows"]):
                images = load_and_preprocess_images(
                    [protocol["views"][i]["path"] for i in indices],
                    image_resolution=480,
                    mode="balanced",
                ).cuda()
                if tuple(images.shape) != (len(indices), 3, 352, 640):
                    raise ValueError("unexpected preprocessing shape")
                torch.cuda.reset_peak_memory_stats()
                torch.cuda.synchronize()
                started = time.perf_counter()
                with (
                    torch.inference_mode(),
                    sdpa_kernel(
                        [SDPBackend.FLASH_ATTENTION, SDPBackend.EFFICIENT_ATTENTION]
                    ),
                ):
                    result = model(images)
                torch.cuda.synchronize()
                seconds = time.perf_counter() - started
                extrinsic, intrinsic = encoding_to_camera(
                    result["pose_enc"], images.shape[-2:]
                )
                arrays = {
                    "depth": result["depth"][0, ..., 0].float().cpu().numpy(),
                    "confidence": result["depth_conf"][0].float().cpu().numpy(),
                    "pose_enc": result["pose_enc"][0].float().cpu().numpy(),
                    "extrinsic": extrinsic[0].float().cpu().numpy(),
                    "intrinsic": intrinsic[0].float().cpu().numpy(),
                }
                for array in arrays.values():
                    if not np.isfinite(array).all():
                        raise ValueError("nonfinite prediction")
                output = destination / f"{index:02d}.npz"
                np.savez_compressed(output, **arrays)
                record["windows"].append(
                    {
                        "index": index,
                        "indices": indices,
                        "seconds": seconds,
                        "sha256": digest(output),
                        "path": str(output.relative_to(root)),
                        "peak_allocated_mib": torch.cuda.max_memory_allocated()
                        / 1048576,
                        "peak_reserved_mib": torch.cuda.max_memory_reserved() / 1048576,
                        "shapes": {k: v.shape for k, v in arrays.items()},
                    }
                )
                record["forward_calls"] += 1
                write_json(root / "sweeps-inference-progress.json", record)
                print(
                    f"Window {index + 1}/{len(protocol['windows'])}: {seconds:.2f}s; {record['windows'][-1]['peak_allocated_mib']:.0f} MiB",
                    flush=True,
                )
                del images, result, extrinsic, intrinsic, arrays
                torch.cuda.empty_cache()
            record["status"] = "completed"
        except Exception as error:
            record.update(status="failed", error=str(error))
            raise
        finally:
            write_json(root / "sweeps-inference.json", record)
    write_json(root / "sweeps-resource-monitor.json", monitor.summary())
    write_json(
        root / "sweeps-environment.json",
        {
            "python": sys.version,
            "torch_cuda": torch.version.cuda,
            "visible_devices": os.environ["CUDA_VISIBLE_DEVICES"],
            "packages": {
                d.metadata["Name"]: d.version
                for d in importlib.metadata.distributions()
            },
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("freeze", "infer"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("/home/hehao/.cache/ai-annotation-platform/nuscenes-mini/content"),
    )
    parser.add_argument(
        "--archive",
        type=Path,
        default=Path(
            "/home/hehao/.cache/ai-annotation-platform/nuscenes-mini/v1.0-mini.tgz"
        ),
    )
    parser.add_argument("--scene", default="scene-0061")
    parser.add_argument(
        "--previous",
        type=Path,
        default=Path(
            ".artifacts/model-review/vggt-omega-lidar-densification/full-sequence"
        ),
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("/home/hehao/.x-repo/github.com/facebookresearch/vggt-omega"),
    )
    parser.add_argument("--weights", type=Path)
    args = parser.parse_args()
    globals()[args.command](args)


if __name__ == "__main__":
    main()
