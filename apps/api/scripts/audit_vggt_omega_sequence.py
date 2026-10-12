"""Independently inspect saved full-scene inputs, provenance and display assets."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import numpy as np
from PIL import Image

from omega_densification import cloud_depth_at_targets
from qualify_vggt_omega import CAMERAS, SceneData, digest
from vggt_common import points_in_actors, verify_runtime_sources, write_json


def audit(args):
    started = time.perf_counter()
    root = args.output
    protocol = json.loads((root / "sequence-selected-protocol.json").read_text())
    build_protocol = json.loads((root / "sequence-build-protocol.json").read_text())
    build = json.loads((root / "sequence-build.json").read_text())
    display = json.loads((root / "sequence-data.json").read_text())
    run = Path(build_protocol["prediction_run"])
    inference = json.loads((run / "sequence-inference.json").read_text())
    if build_protocol["input_protocol_sha256"] != digest(
        root / "sequence-selected-protocol.json"
    ):
        raise ValueError("selected input protocol changed")
    if inference["protocol_sha256"] != build_protocol["input_protocol_sha256"]:
        raise ValueError("inference/input provenance is disconnected")
    if inference["status"] != "completed" or inference["forward_calls"] != 1:
        raise ValueError("not one successful joint forward")
    if inference["input_images"] != protocol["frames"] * 6:
        raise ValueError("incomplete six-camera sequence")
    if build["protocol_sha256"] != digest(root / "sequence-build-protocol.json"):
        raise ValueError("build protocol changed")
    if (
        digest(root / "build-source" / "build_vggt_omega_sequence.py")
        != build_protocol["source_sha256"]
    ):
        raise ValueError("build source snapshot changed")
    for name, expected in protocol["source_hashes"].items():
        if digest(run / "inference-source" / name) != expected:
            raise ValueError("inference source snapshot changed")
    for name, expected in build_protocol["metadata_sha256"].items():
        if digest(Path(protocol["data_root"]) / "v1.0-mini" / name) != expected:
            raise ValueError("metadata changed")
    for row in [*protocol["views"], *protocol["scans"]]:
        if digest(row["path"]) != row["sha256"]:
            raise ValueError("sensor input changed")
    for frame in range(protocol["frames"]):
        if [v["camera"] for v in protocol["views"] if v["frame"] == frame] != list(
            CAMERAS
        ):
            raise ValueError("missing or duplicate camera exposure")
    if len({v["token"] for v in protocol["views"]}) != protocol["input_images"]:
        raise ValueError("duplicate RGB exposure")
    heldout = {protocol["scans"][i]["token"] for i in protocol["heldout_frames"]}
    for row in build["views"]:
        if heldout.intersection(row["anchor_scan_tokens"]):
            raise ValueError("heldout scan leaked into calibration")
        if (
            row["raw_points"] + row["actor_points"] <= 0
            or row["accepted_points"] + row["actor_points"] <= 0
        ):
            raise ValueError(
                "an input exposure produced neither static nor frame-local geometry"
            )
    predictions = {}
    for name, record in inference["files"].items():
        path = run / "sequence-predictions" / f"{name}.npy"
        if digest(path) != record["sha256"]:
            raise ValueError("prediction changed")
        predictions[name] = np.load(path, mmap_mode="r")
        if (
            list(predictions[name].shape) != record["shape"]
            or not np.isfinite(predictions[name]).all()
        ):
            raise ValueError("invalid prediction shape/value")
    data = SceneData(Path(protocol["data_root"]))
    scan_cache = {}
    origin = np.asarray(display["origin"])
    basis = np.asarray(display["viewer_from_world"])
    if abs(np.linalg.det(basis) - 1) > 1e-12:
        raise ValueError("viewer is reflected")
    pose_scale = build["raw_pose_alignment"]["alignment_scale"]
    pose_rotation = np.asarray(build["raw_pose_alignment"]["alignment_rotation"])
    pose_translation = np.asarray(build["raw_pose_alignment"]["alignment_translation"])
    grid_w, grid_h = display["grid"]
    sampled, maximum_xyz, maximum_pixel = 0, 0.0, 0.0
    clouds = {}
    counts = {}
    random = np.random.default_rng(19)
    for method, asset in display["assets"].items():
        path = root / asset["full_file"]
        if digest(path) != asset["full_sha256"]:
            raise ValueError("full cloud changed")
        with np.load(path) as saved:
            cloud = {
                name: saved[name] for name in ("xyz", "rgb", "source", "pixel", "kind")
            }
        clouds[method] = cloud
        size = len(cloud["xyz"])
        if size != asset["points"] or not np.isfinite(cloud["xyz"]).all():
            raise ValueError("invalid saved cloud")
        if any(len(v) != size for v in cloud.values()):
            raise ValueError("attribute length mismatch")
        source = cloud["source"]
        if np.any(source >= len(protocol["views"])) or np.any(
            source < -protocol["frames"]
        ):
            raise ValueError("invalid source identity")
        if np.any((source >= 0) != (cloud["kind"] == 1)):
            raise ValueError("source provenance kind mismatch")
        if any(np.any(source == -frame - 1) for frame in protocol["heldout_frames"]):
            raise ValueError("heldout LiDAR leaked into a saved cloud")
        for identity in np.unique(source):
            rows = np.flatnonzero(source == identity)
            take = rows[random.choice(len(rows), min(120, len(rows)), replace=False)]
            pixel = cloud["pixel"][take]
            if identity < 0:
                frame = -int(identity) - 1
                if frame not in scan_cache:
                    scan_cache[frame] = data.scan(protocol["scene"], frame)[0]
                expected = scan_cache[frame][pixel]
            else:
                i = int(identity)
                x, y = pixel % grid_w, pixel // grid_w
                if np.any((x < 0) | (x >= grid_w) | (y < 0) | (y >= grid_h)):
                    raise ValueError("invalid source pixel")
                with np.load(root / "views" / f"{i:03d}.npz") as saved:
                    valid = saved["raw_valid" if method == "raw" else "accepted"][y, x]
                    if not valid.all():
                        raise ValueError("cloud pixel was not accepted")
                    if method == "raw":
                        extrinsic = predictions["extrinsic"][i]
                        rotation = extrinsic[:3, :3].astype(float).T
                        translation = -rotation @ extrinsic[:3, 3]
                        rotation = pose_rotation @ rotation
                        translation = (
                            pose_rotation @ translation * pose_scale + pose_translation
                        )
                        intrinsic = np.array(predictions["intrinsic"][i], dtype=float)
                        intrinsic[:2, 2] -= 0.5
                        z = predictions["depth"][i, y, x] * pose_scale
                    else:
                        pose = np.asarray(protocol["views"][i]["global_from_sensor"])
                        rotation, translation = pose[:3, :3], pose[:3, 3]
                        intrinsic = np.asarray(protocol["views"][i]["intrinsic"])
                        z = saved["corrected"][y, x]
                rays = (
                    np.column_stack((x, y, np.ones(len(x))))
                    @ np.linalg.inv(intrinsic).T
                )
                expected = (rays * z[:, None]) @ rotation.T + translation
                camera = (cloud["xyz"][take].astype(float) - translation) @ rotation
                projected = camera @ intrinsic.T
                projected = projected[:, :2] / projected[:, 2:]
                error = np.linalg.norm(projected - np.column_stack((x, y)), axis=1)
                maximum_pixel = max(maximum_pixel, float(error.max(initial=0)))
                if not np.array_equal(cloud["rgb"][take], predictions["rgb"][i, y, x]):
                    raise ValueError("point RGB differs from its source input")
            maximum_xyz = max(
                maximum_xyz,
                float(
                    np.max(
                        np.linalg.norm(cloud["xyz"][take] - expected, axis=1), initial=0
                    )
                ),
            )
            sampled += len(take)
        for budget, lod in asset["lod"].items():
            path = root / lod["path"]
            if (
                digest(path) != lod["sha256"]
                or path.stat().st_size != lod["points"] * 16
            ):
                raise ValueError("LOD bytes changed")
            indices = np.load(root / f"{method}-{budget}-indices.npy")
            packed = np.fromfile(
                path, dtype=[("xyz", "<f4", (3,)), ("rgb", "u1", (3,)), ("kind", "u1")]
            )
            expected = (
                (cloud["xyz"][indices].astype(float) - origin) @ basis.T
            ).astype(np.float32)
            if not np.array_equal(packed["xyz"], expected) or not np.array_equal(
                packed["rgb"], cloud["rgb"][indices]
            ):
                raise ValueError("display geometry differs from full saved samples")
        counts[method] = size
        print(f"Audited {method}: {size:,} complete points", flush=True)
    if maximum_xyz > 0.0003 or maximum_pixel > 0.15:
        raise ValueError(
            f"source reconstruction mismatch: {maximum_xyz}m / {maximum_pixel}px"
        )
    real = clouds["fused"]["kind"] == 0
    for name in clouds["measured"]:
        if not np.array_equal(clouds["fused"][name][real], clouds["measured"][name]):
            raise ValueError("fusion changed measured returns")
    actor_count = 0
    for frame, asset in enumerate(display["actors"]):
        path = root / "frames" / asset["path"]
        if (
            digest(path) != asset["sha256"]
            or path.stat().st_size != asset["points"] * 16
        ):
            raise ValueError("actor display changed")
        with np.load(root / "frames" / f"actors-{frame:02d}.npz") as saved:
            if not np.all(saved["source"] // 6 == frame) or not np.all(
                saved["kind"] == 2
            ):
                raise ValueError("actors from another time were accumulated")
            for i in np.unique(saved["source"]):
                rows = np.flatnonzero(saved["source"] == i)
                take = rows[
                    random.choice(len(rows), min(300, len(rows)), replace=False)
                ]
                view = protocol["views"][int(i)]
                if not points_in_actors(
                    saved["xyz"][take],
                    data.boxes(protocol["scene"], view["timestamp"]),
                    0.3002,
                ).all():
                    raise ValueError("frame-local actor candidate outside oracle box")
            actor_count += len(saved["xyz"])
    for i in range(protocol["input_images"]):
        if digest(root / "rgb" / f"{i:03d}.jpg") != protocol["views"][i]["sha256"]:
            raise ValueError("copied RGB differs")
        with Image.open(root / "frames" / f"render-{i:03d}.jpg") as image:
            if image.size != tuple(display["grid"]):
                raise ValueError("render dimensions differ")
    checked_scores = 0
    with np.load(root / "sequence-scored-depths.npz") as scores:
        if len(scores["truth"]) != display["validation"]["targets"]:
            raise ValueError("validation denominator changed")
        truth = scores["truth"]
        for method in counts:
            values = scores[method]
            valid = np.isfinite(values)
            metric = display["validation"]["metrics"][method]
            expected = np.mean(valid & (np.abs(values - truth) <= 0.5))
            if not np.isclose(
                expected, metric["correct_coverage_0.5m"], rtol=0, atol=1e-14
            ):
                raise ValueError("fixed-denominator metric differs from saved depths")
            if not np.isclose(
                np.mean(np.abs(values[valid] - truth[valid])),
                metric["mae_m"],
                rtol=0,
                atol=1e-12,
            ):
                raise ValueError("saved depth MAE differs")
        offset = 0
        for frame in protocol["heldout_frames"]:
            with np.load(root / f"targets-{frame:02d}.npz") as target:
                size = len(target["truth_z"])
                if len(np.unique(target["raw_rows"])) != size:
                    raise ValueError("duplicate heldout return")
                if not np.array_equal(truth[offset : offset + size], target["truth_z"]):
                    raise ValueError("target truth differs from scored truth")
                source_xyz = data.scan(protocol["scene"], frame)[0][target["raw_rows"]]
                if not np.allclose(source_xyz, target["xyz"], rtol=0, atol=1e-10):
                    raise ValueError("target differs from raw heldout acquisition")
                for cam in (0, 3):
                    index = frame * 6 + cam
                    rows = np.flatnonzero(target["view"] == index)[:20]
                    view = protocol["views"][index]
                    for method in clouds:
                        fresh = cloud_depth_at_targets(
                            clouds[method]["xyz"],
                            target["uv"][rows],
                            np.asarray(view["intrinsic"]),
                            np.asarray(view["global_from_sensor"]),
                            (grid_h, grid_w),
                            radius=1.5,
                        )
                        if not np.allclose(
                            fresh,
                            scores[method][offset + rows],
                            rtol=0,
                            atol=1e-10,
                            equal_nan=True,
                        ):
                            raise ValueError("sampled saved-cloud score differs")
                        checked_scores += len(rows)
                offset += size
        if offset != len(truth):
            raise ValueError("heldout target concatenation differs")
    ui = (
        Path(__file__).resolve().parents[2]
        / "web"
        / "scripts"
        / "vggt-omega-sequence-ui"
    )
    ui_hashes = {}
    for source in ui.iterdir():
        if source.is_file():
            if digest(source) != digest(root / source.name):
                raise ValueError("live UI differs from served UI")
            ui_hashes[source.name] = digest(source)
    preserved = verify_runtime_sources(root.parent)
    shutil.copyfile(__file__, root / "sequence-audit-source.py")
    record = {
        "passed": True,
        "scene_keyframes": protocol["frames"],
        "joint_rgb_inputs": protocol["input_images"],
        "retained_geometry_views": len(build["views"]),
        "retained_static_views": build["source_views_with_calibrated_points"],
        "actor_only_views": [
            row["index"]
            for row in build["views"]
            if row["accepted_points"] == 0 and row["actor_points"] > 0
        ],
        "whole_scan_holdouts": len(heldout),
        "saved_static_points": counts,
        "saved_frame_local_actor_points": actor_count,
        "sampled_source_points": sampled,
        "independently_reprojected_score_samples": checked_scores,
        "max_source_xyz_error_m": maximum_xyz,
        "max_source_reprojection_error_px": maximum_pixel,
        "source_rgb_files": protocol["input_images"],
        "rendered_rgb_files": protocol["input_images"],
        "old_consolidation_source_fence_preserved": preserved,
        "ui_hashes": ui_hashes,
        "audit_source_sha256": digest(__file__),
        "seconds": time.perf_counter() - started,
    }
    write_json(root / "sequence-artifact-audit.json", record)
    print(json.dumps(record), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    audit(parser.parse_args())


if __name__ == "__main__":
    main()
