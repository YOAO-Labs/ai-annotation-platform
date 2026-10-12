"""Freeze and infer one complete six-camera annotation scene with VGGT-Omega."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

from omega_densification import resized_intrinsic
from qualify_vggt_omega import (
    CAMERAS,
    SOURCE_COMMIT,
    WEIGHT_REVISION,
    SceneData,
    digest,
)
from vggt_common import MemoryMonitor, write_json

WEIGHT_SHA = "c02da418b18bb01d0392598d3f6147366bcde1bb70fd08a5e3bf7925b0667934"
HELDOUT = (8, 19, 30)


def freeze(args):
    root = args.output
    root.mkdir(parents=True, exist_ok=True)
    destination = root / "sequence-protocol.json"
    if destination.exists():
        raise ValueError("the input protocol is already frozen")
    data = SceneData(args.data)
    patches = args.resolution / 16
    grid = (
        int(np.round(patches / np.sqrt(900 / 1600))) * 16,
        int(np.round(patches * np.sqrt(900 / 1600))) * 16,
    )
    frames = data.frames[args.scene]
    views, scans = [], []
    for frame, channels in enumerate(frames):
        for camera in CAMERAS:
            row = channels[camera]
            calibration = data.tables["calibrated_sensor"][
                row["calibrated_sensor_token"]
            ]
            view = data.record(row)
            with Image.open(view["path"]) as image:
                if image.size != (1600, 900):
                    raise ValueError("unsupported source image shape")
            views.append(
                {
                    **view,
                    "frame": frame,
                    "camera": camera,
                    "intrinsic": resized_intrinsic(
                        np.asarray(calibration["camera_intrinsic"]), (1600, 900), grid
                    ),
                }
            )
        scan = data.record(channels["LIDAR_TOP"])
        scans.append({**scan, "frame": frame, "heldout": frame in HELDOUT})
    sample_tokens = {channels["LIDAR_TOP"]["sample_token"] for channels in frames}
    camera_rows = []
    for row in data.tables["sample_data"].values():
        if row["sample_token"] not in sample_tokens:
            continue
        sensor = data.tables["sensor"][
            data.tables["calibrated_sensor"][row["calibrated_sensor_token"]][
                "sensor_token"
            ]
        ]
        if sensor["modality"] == "camera":
            camera_rows.append(row)
    sources = {}
    snapshot = root / "inference-source"
    snapshot.mkdir()
    for name in (
        Path(__file__).name,
        "qualify_vggt_omega.py",
        "omega_densification.py",
        "vggt_common.py",
        "import_nuscenes_scene.py",
    ):
        source = Path(__file__).with_name(name)
        (snapshot / name).write_bytes(source.read_bytes())
        sources[name] = digest(source)
    write_json(
        destination,
        {
            "scene": args.scene,
            "data_root": args.data.resolve(),
            "scope": "all annotation keyframes, every keyframe has all six cameras; camera sweeps between keyframes are not inputs",
            "frames": len(frames),
            "cameras": CAMERAS,
            "input_images": len(views),
            "available_camera_metadata_rows": len(camera_rows),
            "non_keyframe_camera_metadata_rows": sum(
                not row["is_key_frame"] for row in camera_rows
            ),
            "source_commit": SOURCE_COMMIT,
            "weight_repo": "facebook/VGGT-Omega",
            "weight_file": "vggt_omega_1b_512.pt",
            "weight_revision": WEIGHT_REVISION,
            "weight_sha256": WEIGHT_SHA,
            "preprocess": {
                "resolution": args.resolution,
                "mode": "balanced",
                "size": grid,
                "pixel_centres": "integer array indices, PIL half-pixel K affine; predicted K principal point minus 0.5 matches upstream training pixel centres",
            },
            "inference": "one joint forward, all input images; Flash or efficient SDPA only, no math fallback",
            "heldout_frames": HELDOUT,
            "calibration": "only scans within two keyframes of the RGB exposure; all heldout scans excluded globally",
            "selection": {
                "confidence_quantile": 0.2,
                "range_m": [1, 80],
                "voxel_m": 0.1,
                "actor_padding_m": 0.3,
                "bottom_fraction_removed": 0.05,
                "cross_view_absolute_m": 0.3,
                "cross_view_relative": 0.03,
                "cross_view_cycle_px": 2,
            },
            "views": views,
            "scans": scans,
            "source_hashes": sources,
        },
    )
    print(
        f"Frozen {args.scene}: {len(frames)} keyframes, {len(views)} RGB inputs, {len(scans)} scans",
        flush=True,
    )


def infer(args):
    if os.environ.get("CUDA_VISIBLE_DEVICES") != "1":
        raise ValueError("only physical GPU 1 is authorized for this experiment")
    root = args.output
    protocol = json.loads((root / "sequence-protocol.json").read_text())
    if (root / "sequence-inference.json").exists():
        raise ValueError("preserve the previous inference record")
    if digest(args.weights) != protocol["weight_sha256"]:
        raise ValueError("checkpoint SHA mismatch")
    commit = subprocess.check_output(
        ["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(args.source), "status", "--porcelain"], text=True
    ).strip()
    if commit != SOURCE_COMMIT or dirty:
        raise ValueError("upstream source is not clean and pinned")
    for name, expected in protocol["source_hashes"].items():
        if digest(Path(__file__).with_name(name)) != expected:
            raise ValueError(f"frozen inference source changed: {name}")
    for view in protocol["views"]:
        if digest(view["path"]) != view["sha256"]:
            raise ValueError("RGB input changed")
    sys.path.insert(0, str(args.source))
    import torch
    from torch.nn.attention import SDPBackend, sdpa_kernel
    from vggt_omega.models import VGGTOmega
    from vggt_omega.utils.load_fn import load_and_preprocess_images
    from vggt_omega.utils.pose_enc import encoding_to_camera

    torch.set_num_threads(8)
    torch.manual_seed(42)
    np.random.seed(42)
    destination = root / "sequence-predictions"
    destination.mkdir()
    record = {
        "protocol_sha256": digest(root / "sequence-protocol.json"),
        "source_commit": commit,
        "weight_sha256": WEIGHT_SHA,
        "forward_calls": 0,
        "input_images": len(protocol["views"]),
        "physical_gpu": 1,
    }
    with MemoryMonitor(1) as monitor:
        started = time.perf_counter()
        model = VGGTOmega().eval()
        model.load_state_dict(
            torch.load(args.weights, map_location="cpu", weights_only=True), strict=True
        )
        model = model.cuda()
        print(f"Loaded model in {time.perf_counter() - started:.1f}s", flush=True)
        images = load_and_preprocess_images(
            [v["path"] for v in protocol["views"]],
            image_resolution=protocol["preprocess"]["resolution"],
            mode=protocol["preprocess"]["mode"],
        ).cuda()
        width, height = protocol["preprocess"]["size"]
        if tuple(images.shape) != (len(protocol["views"]), 3, height, width):
            raise ValueError(f"unexpected image grid {images.shape}")
        hooks = []
        for index in (0, 5, 11, 17, 23):
            hooks.append(
                model.aggregator.inter_frame_blocks[index].register_forward_hook(
                    lambda module, inputs, output, index=index: print(
                        f"Global encoder block {index + 1}/24 completed", flush=True
                    )
                )
            )
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()
        started = time.perf_counter()
        try:
            with (
                torch.inference_mode(),
                sdpa_kernel(
                    [SDPBackend.FLASH_ATTENTION, SDPBackend.EFFICIENT_ATTENTION]
                ),
            ):
                record["forward_calls"] += 1
                result = model(images)
            torch.cuda.synchronize()
            record["seconds"] = time.perf_counter() - started
            extrinsic, intrinsic = encoding_to_camera(
                result["pose_enc"], images.shape[-2:]
            )
            arrays = {
                "depth": result["depth"][0, ..., 0].float().cpu().numpy(),
                "confidence": result["depth_conf"][0].float().cpu().numpy(),
                "pose_enc": result["pose_enc"][0].float().cpu().numpy(),
                "extrinsic": extrinsic[0].float().cpu().numpy(),
                "intrinsic": intrinsic[0].float().cpu().numpy(),
                "rgb": np.rint(images.cpu().permute(0, 2, 3, 1).numpy() * 255)
                .clip(0, 255)
                .astype(np.uint8),
            }
            record["files"] = {}
            for name, array in arrays.items():
                if not np.isfinite(array).all():
                    raise ValueError(f"nonfinite {name}")
                path = destination / f"{name}.npy"
                np.save(path, array)
                record["files"][name] = {"shape": array.shape, "sha256": digest(path)}
            record["status"] = "completed"
            record["peak_allocated_mib"] = torch.cuda.max_memory_allocated() / 1048576
            record["peak_reserved_mib"] = torch.cuda.max_memory_reserved() / 1048576
            print(json.dumps(record), flush=True)
        except Exception as error:
            record.update(
                status="failed", error=str(error), seconds=time.perf_counter() - started
            )
            raise
        finally:
            for hook in hooks:
                hook.remove()
            record["peak_allocated_mib"] = torch.cuda.max_memory_allocated() / 1048576
            record["peak_reserved_mib"] = torch.cuda.max_memory_reserved() / 1048576
            write_json(root / "sequence-inference.json", record)
            write_json(root / "sequence-resource-monitor.json", monitor.summary())
    write_json(root / "sequence-resource-monitor.json", monitor.summary())
    write_json(
        root / "sequence-environment.json",
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
    parser.add_argument("--scene", default="scene-0061")
    parser.add_argument("--resolution", type=int, default=512, choices=(480, 512))
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
