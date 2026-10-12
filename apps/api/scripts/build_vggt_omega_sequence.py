"""Build auditable full-sequence point rendering from one joint prediction."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import numpy as np
from numba import njit
from PIL import Image
from scipy.spatial.transform import Rotation

from omega_densification import (
    cloud_depth_at_targets,
    error_summary,
    lidar_evidence,
    local_correction,
    project,
    robust_scale,
    sample_image,
    unproject,
    visible_rows,
    voxel_representatives,
)
from qualify_vggt_omega import CAMERAS, SceneData, digest
from vggt_common import actor_ray_mask, cross_view_support, points_in_actors, write_json

VIEWER_FROM_WORLD = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], dtype=float)
METHODS = {
    "calibrated": "LiDAR 校准模型",
    "raw": "模型原始几何 · 全局对齐",
    "fused": "实测 + 校准模型",
    "measured": "多帧实测 LiDAR",
}


def anchor_frames(frame, count, heldout):
    """Whole-scan exclusions apply to every exposure in the scene."""
    return [
        f for f in range(max(0, frame - 2), min(count, frame + 3)) if f not in heldout
    ]


def references(index, count):
    frame, camera = divmod(index, 6)
    result = [frame * 6 + (camera - 1) % 6, frame * 6 + (camera + 1) % 6]
    result.extend(f * 6 + camera for f in (frame - 1, frame + 1) if 0 <= f < count)
    return result


def similarity_align(source, target):
    """Robust proper Sim(3); no per-camera warping of the raw model output."""
    source, target = np.asarray(source, float), np.asarray(target, float)
    keep = np.ones(len(source), bool)
    for _ in range(4):
        x, y = source[keep], target[keep]
        mx, my = x.mean(axis=0), y.mean(axis=0)
        x, y = x - mx, y - my
        u, singular, vt = np.linalg.svd(y.T @ x / len(x))
        sign = np.ones(3)
        sign[-1] = np.linalg.det(u @ vt)
        rotation = u @ np.diag(sign) @ vt
        scale = float(np.sum(singular * sign) / np.mean(np.sum(x * x, axis=1)))
        translation = my - scale * rotation @ mx
        residual = np.linalg.norm(
            source @ rotation.T * scale + translation - target, axis=1
        )
        middle = np.median(residual)
        threshold = max(0.5, middle + 3 * 1.4826 * np.median(np.abs(residual - middle)))
        next_keep = residual <= threshold
        if next_keep.sum() < 8 or np.array_equal(next_keep, keep):
            break
        keep = next_keep
    return scale, rotation, translation, residual, keep


def camera_poses(extrinsic):
    poses = np.broadcast_to(np.eye(4), (len(extrinsic), 4, 4)).copy()
    poses[:, :3, :3] = np.transpose(extrinsic[:, :3, :3], (0, 2, 1))
    poses[:, :3, 3] = -np.einsum("nij,nj->ni", poses[:, :3, :3], extrinsic[:, :3, 3])
    return poses


def empty():
    return {
        "xyz": np.empty((0, 3), np.float32),
        "rgb": np.empty((0, 3), np.uint8),
        "source": np.empty(0, np.int16),
        "pixel": np.empty(0, np.int32),
        "kind": np.empty(0, np.uint8),
    }


def combine(items):
    return (
        {k: np.concatenate([x[k] for x in items]) for k in empty()}
        if items
        else empty()
    )


def subset(item, take):
    return {k: v[take] for k, v in item.items()}


def from_depth(depth, rgb, mask, intrinsic, pose, source, kind):
    y, x = np.nonzero(mask)
    xyz = unproject(np.column_stack((x, y)), depth[y, x], intrinsic, pose)
    return {
        "xyz": xyz.astype(np.float32),
        "rgb": rgb[y, x],
        "source": np.full(len(x), source, np.int16),
        "pixel": (y * depth.shape[1] + x).astype(np.int32),
        "kind": np.full(len(x), kind, np.uint8),
    }


def viewer_pose(pose, origin):
    result = np.eye(4)
    result[:3, :3] = VIEWER_FROM_WORLD @ pose[:3, :3]
    result[:3, 3] = VIEWER_FROM_WORLD @ (pose[:3, 3] - origin)
    return result


def packed(path, item, origin):
    count = len(item["xyz"])
    buffer = np.zeros(
        count, dtype=[("xyz", "<f4", (3,)), ("rgb", "u1", (3,)), ("kind", "u1")]
    )
    buffer["xyz"] = (item["xyz"].astype(float) - origin) @ VIEWER_FROM_WORLD.T
    buffer["rgb"], buffer["kind"] = item["rgb"], item["kind"]
    buffer.tofile(path)
    return {
        "path": path.name,
        "points": count,
        "bytes": path.stat().st_size,
        "sha256": digest(path),
    }


def export_cloud(root, name, item, origin):
    take = voxel_representatives(item["xyz"], 0.1)
    result = subset(item, take)
    np.savez_compressed(root / f"{name}-full.npz", **result)
    assets = {}
    for budget in (120000, 350000, 1200000):
        indices = np.linspace(0, len(take) - 1, min(len(take), budget), dtype=np.int64)
        asset = packed(root / f"{name}-{budget}.bin", subset(result, indices), origin)
        np.save(root / f"{name}-{budget}-indices.npy", indices)
        assets[str(budget)] = asset
    record = {
        "points": len(take),
        "before_voxel": len(item["xyz"]),
        "full_file": f"{name}-full.npz",
        "full_sha256": digest(root / f"{name}-full.npz"),
        "lod": assets,
        "source_counts": {
            str(int(v)): int(n)
            for v, n in zip(*np.unique(result["source"], return_counts=True))
        },
    }
    return result, record


def fixed_targets(data, protocol, masks):
    h, w = protocol["preprocess"]["size"][::-1]
    results = []
    for frame in protocol["heldout_frames"]:
        xyz, scan = data.scan(protocol["scene"], frame)
        actor = points_in_actors(
            xyz, data.boxes(protocol["scene"], scan["timestamp"]), 0.3
        )
        assignment = np.full(len(xyz), -1, np.int16)
        best = np.full(len(xyz), -np.inf)
        target_uv = np.zeros((len(xyz), 2))
        target_z = np.zeros(len(xyz))
        for i in range(frame * 6, frame * 6 + 6):
            view = protocol["views"][i]
            uv, z = project(
                xyz,
                np.asarray(view["intrinsic"]),
                np.asarray(view["global_from_sensor"]),
            )
            take = visible_rows(uv, z, (h, w))
            pixels = np.rint(uv[take]).astype(int)
            take = take[~actor[take] & ~masks[i, pixels[:, 1], pixels[:, 0]]]
            margin = np.minimum.reduce(
                (
                    uv[take, 0],
                    w - 1 - uv[take, 0],
                    uv[take, 1],
                    0.95 * h - 1 - uv[take, 1],
                )
            )
            better = margin > best[take]
            rows = take[better]
            assignment[rows], best[rows] = i, margin[better]
            target_uv[rows], target_z[rows] = uv[rows], z[rows]
        take = assignment >= 0
        results.append(
            {
                "frame": frame,
                "scan_token": scan["token"],
                "xyz": xyz[take],
                "raw_rows": np.flatnonzero(take),
                "view": assignment[take],
                "uv": target_uv[take],
                "truth_z": target_z[take],
            }
        )
    return results


@njit(cache=False, nogil=True)
def raster_points(xyz, rgb, camera_from_world, intrinsic, height, width, radius):
    """CPU depth-tested colored points, without invented hole filling."""
    image = np.full((height, width, 3), 255, np.uint8)
    zbuffer = np.full((height, width), np.inf, np.float64)
    for i in range(len(xyz)):
        x, y, z = xyz[i]
        cx = (
            camera_from_world[0, 0] * x
            + camera_from_world[0, 1] * y
            + camera_from_world[0, 2] * z
            + camera_from_world[0, 3]
        )
        cy = (
            camera_from_world[1, 0] * x
            + camera_from_world[1, 1] * y
            + camera_from_world[1, 2] * z
            + camera_from_world[1, 3]
        )
        cz = (
            camera_from_world[2, 0] * x
            + camera_from_world[2, 1] * y
            + camera_from_world[2, 2] * z
            + camera_from_world[2, 3]
        )
        if not np.isfinite(cz) or cz <= 0.05 or cz >= 160:
            continue
        u = (intrinsic[0, 0] * cx + intrinsic[0, 1] * cy) / cz + intrinsic[0, 2]
        v = (intrinsic[1, 0] * cx + intrinsic[1, 1] * cy) / cz + intrinsic[1, 2]
        if (
            not np.isfinite(u)
            or not np.isfinite(v)
            or u < 0
            or u >= width
            or v < 0
            or v >= height
        ):
            continue
        pu, pv = int(np.rint(u)), int(np.rint(v))
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                a, b = pu + dx, pv + dy
                if 0 <= a < width and 0 <= b < height and cz < zbuffer[b, a]:
                    zbuffer[b, a] = cz
                    image[b, a] = rgb[i]
    return image


def render_points(item, view, shape, radius=1):
    return raster_points(
        item["xyz"],
        item["rgb"],
        np.linalg.inv(np.asarray(view["global_from_sensor"])),
        np.asarray(view["intrinsic"]),
        shape[0],
        shape[1],
        radius,
    )


def build(args):
    started = time.perf_counter()
    root = args.output
    prediction_run = args.prediction_run or root
    protocol_path = prediction_run / "sequence-protocol.json"
    protocol = json.loads(protocol_path.read_text())
    inference = json.loads((prediction_run / "sequence-inference.json").read_text())
    if inference["protocol_sha256"] != digest(protocol_path):
        raise ValueError("inference protocol mismatch")
    if inference["status"] != "completed" or inference["forward_calls"] != 1:
        raise ValueError("expected a completed single joint inference")
    if (root / "sequence-data.json").exists():
        raise ValueError("preserve the previous reconstruction")
    snapshot = root / "build-source"
    snapshot.mkdir()
    shutil.copyfile(__file__, snapshot / Path(__file__).name)
    write_json(
        root / "sequence-build-protocol.json",
        {
            "source_sha256": digest(__file__),
            "input_protocol_sha256": digest(protocol_path),
            "prediction_record_sha256": digest(
                prediction_run / "sequence-inference.json"
            ),
            "prediction_run": prediction_run.resolve(),
            "raw_alignment": "robust proper Sim3 on all predicted/known camera centres; only one scale/rotation/translation",
            "raw_intrinsic": "subtract .5 from predicted principal point for integer-centre unprojection",
            "cross_view_references": "same camera in adjacent keyframes, and two neighboring cameras in the same frame; heldout LiDAR not used",
            "filtered_rule": "confidence and static mask and range, (one model support or direct LiDAR agreement), zero model visible conflicts, no LiDAR free-space conflict",
            "voxel": "0.1m, preserve first real sample; measured points precede generated points in fusion",
            "display_lod_budgets": [120000, 350000, 1200000],
            "target_footprint_px": 1.5,
            "render_point_radius_px": 1,
            "metadata_sha256": {
                path.name: digest(path)
                for path in (Path(protocol["data_root"]) / "v1.0-mini").glob("*.json")
            },
            "actors": "frame-local candidates must also lie inside an oracle actor box at their exact RGB exposure; no motion compensation",
        },
    )
    data = SceneData(Path(protocol["data_root"]))
    shutil.copyfile(protocol_path, root / "sequence-selected-protocol.json")
    for row in [*protocol["views"], *protocol["scans"]]:
        if digest(row["path"]) != row["sha256"]:
            raise ValueError("a frozen sensor file changed")
        current = data.tables["sample_data"][row["token"]]
        if not np.allclose(
            data.pose(current), row["global_from_sensor"], rtol=0, atol=1e-10
        ):
            raise ValueError("a frozen sensor pose changed")
    p = prediction_run / "sequence-predictions"
    arrays = {
        name: np.load(p / f"{name}.npy", mmap_mode="r") for name in inference["files"]
    }
    for name in arrays:
        if digest(p / f"{name}.npy") != inference["files"][name]["sha256"]:
            raise ValueError("saved prediction changed")
    depth, confidence, rgb = arrays["depth"], arrays["confidence"], arrays["rgb"]
    h, w = depth.shape[1:]
    views, count = protocol["views"], protocol["frames"]
    known_poses = np.asarray([v["global_from_sensor"] for v in views])
    known_k = np.asarray([v["intrinsic"] for v in views])
    predicted_poses = camera_poses(arrays["extrinsic"])
    predicted_k = np.array(arrays["intrinsic"], copy=True)
    predicted_k[:, :2, 2] -= 0.5
    scale_pose, rotation, translation, pose_error, fit_rows = similarity_align(
        predicted_poses[:, :3, 3], known_poses[:, :3, 3]
    )
    aligned_poses = predicted_poses.copy()
    aligned_poses[:, :3, :3] = rotation @ predicted_poses[:, :3, :3]
    aligned_poses[:, :3, 3] = (
        predicted_poses[:, :3, 3] @ rotation.T * scale_pose + translation
    )
    rotation_error = (
        Rotation.from_matrix(
            np.transpose(known_poses[:, :3, :3], (0, 2, 1)) @ aligned_poses[:, :3, :3]
        ).magnitude()
        * 180
        / np.pi
    )
    origin = known_poses[0, :3, 3].copy()
    origin[2] = np.median(known_poses[:, 2, 3]) - 1.5
    (root / "views").mkdir()
    (root / "frames").mkdir()
    (root / "rgb").mkdir()
    masks = np.zeros(depth.shape, bool)
    anchors, measured_frames = [], {}
    heldout = set(protocol["heldout_frames"])
    for scan in protocol["scans"]:
        if scan["frame"] in heldout:
            continue
        if digest(scan["path"]) != scan["sha256"]:
            raise ValueError("LiDAR input changed")
        xyz, row = data.scan(protocol["scene"], scan["frame"])
        keep = ~points_in_actors(
            xyz, data.boxes(protocol["scene"], row["timestamp"]), 0.3
        )
        keep &= (
            np.linalg.norm(xyz - np.asarray(scan["global_from_sensor"])[:3, 3], axis=1)
            <= 80
        )
        rows = np.flatnonzero(keep)
        measured_frames[scan["frame"]] = {
            "xyz": xyz[rows].astype(np.float32),
            "rgb": np.full((len(rows), 3), 150, np.uint8),
            "source": np.full(len(rows), -scan["frame"] - 1, np.int16),
            "pixel": rows.astype(np.int32),
            "kind": np.zeros(len(rows), np.uint8),
        }
    obs_depth, obs_truth, obs_ids = [], [], []
    per_view = []
    for i, view in enumerate(views):
        masks[i] = actor_ray_mask(
            known_k[i],
            (w, h),
            known_poses[i],
            data.boxes(protocol["scene"], view["timestamp"]),
            0.3,
        )
        selected = anchor_frames(view["frame"], count, heldout)
        cloud = combine([measured_frames[f] for f in selected])
        uv, z = project(cloud["xyz"], known_k[i], known_poses[i])
        take = visible_rows(uv, z, (h, w))
        pixel = np.rint(uv[take]).astype(int)
        take = take[~masks[i, pixel[:, 1], pixel[:, 0]]]
        identity = (-(cloud["source"][take].astype(np.int64) + 1)) * 1000000 + cloud[
            "pixel"
        ][take]
        anchor = {"uv": uv[take], "z": z[take], "identities": identity}
        anchors.append(anchor)
        obs_depth.append(sample_image(depth[i], anchor["uv"]))
        obs_truth.append(anchor["z"])
        obs_ids.append(identity)
        per_view.append(
            {
                "index": i,
                "frame": view["frame"],
                "camera": view["camera"],
                "anchor_frames": selected,
                "anchor_scan_tokens": [protocol["scans"][f]["token"] for f in selected],
                "anchor_observations": len(take),
            }
        )
        if i % 30 == 0:
            print(f"Anchor preparation {i + 1}/{len(views)}", flush=True)
    depth_scale, scale_stats = robust_scale(
        np.concatenate(obs_depth), np.concatenate(obs_truth), np.concatenate(obs_ids)
    )
    corrected = np.zeros(depth.shape, np.float32)
    local = np.zeros(depth.shape, bool)
    for i in range(len(views)):
        corrected[i], local[i], stats = local_correction(
            np.array(depth[i]) * depth_scale, rgb[i], anchors[i]["uv"], anchors[i]["z"]
        )
        per_view[i]["local"] = stats
        if i % 30 == 0:
            print(f"Local calibration {i + 1}/{len(views)}", flush=True)
    static_depth = np.where(masks, np.nan, corrected)
    raw_items, calibrated_items, actor_frames = [], [], []
    actor_current = []
    for i, view in enumerate(views):
        conf = confidence[i] >= np.quantile(confidence[i], 0.2)
        conf[int(0.95 * h) :] = False
        raw_z = np.array(depth[i]) * scale_pose
        raw_valid = conf & ~masks[i] & (raw_z >= 1) & (raw_z <= 80)
        raw_items.append(
            from_depth(raw_z, rgb[i], raw_valid, predicted_k[i], aligned_poses[i], i, 1)
        )
        support, conflict = cross_view_support(
            corrected[i],
            known_k[i],
            known_poses[i],
            [
                {
                    "model": static_depth[j],
                    "intrinsic": known_k[j],
                    "reference_from_camera": known_poses[j],
                }
                for j in references(i, count)
            ],
            absolute_m=0.3,
            relative=0.03,
            pixel_tolerance=2,
        )
        direct, lidar_conflict = lidar_evidence(
            corrected[i], anchors[i]["uv"], anchors[i]["z"]
        )
        valid = conf & ~masks[i] & (corrected[i] >= 1) & (corrected[i] <= 80)
        accepted = valid & ((support >= 1) | direct) & (conflict == 0) & ~lidar_conflict
        calibrated_items.append(
            from_depth(corrected[i], rgb[i], accepted, known_k[i], known_poses[i], i, 1)
        )
        actor_valid = conf & masks[i] & (corrected[i] >= 1) & (corrected[i] <= 80)
        actor_candidate = from_depth(
            corrected[i], rgb[i], actor_valid, known_k[i], known_poses[i], i, 2
        )
        inside_actor = points_in_actors(
            actor_candidate["xyz"],
            data.boxes(protocol["scene"], view["timestamp"]),
            0.3,
        )
        actor_candidate = subset(actor_candidate, inside_actor)
        actor_valid[:] = False
        actor_valid.flat[actor_candidate["pixel"]] = True
        actor_current.append(actor_candidate)
        np.savez_compressed(
            root / "views" / f"{i:03d}.npz",
            corrected=corrected[i],
            accepted=accepted,
            raw_valid=raw_valid,
            actor_valid=actor_valid,
            actor_mask=masks[i],
            support=support,
            conflict=conflict,
            lidar_conflict=lidar_conflict,
            local_supported=local[i],
        )
        per_view[i].update(
            raw_points=int(raw_valid.sum()),
            accepted_points=int(accepted.sum()),
            actor_points=int(actor_valid.sum()),
            cross_view_indices=references(i, count),
            free_space_conflicts=int((valid & lidar_conflict).sum()),
        )
        source = Path(view["path"])
        shutil.copyfile(source, root / "rgb" / f"{i:03d}.jpg")
        if i % 6 == 5:
            actor_item = combine(actor_current)
            actor_item = subset(
                actor_item, voxel_representatives(actor_item["xyz"], 0.1)
            )
            actor_frames.append(
                packed(
                    root / "frames" / f"actors-{view['frame']:02d}.bin",
                    actor_item,
                    origin,
                )
            )
            np.savez_compressed(
                root / "frames" / f"actors-{view['frame']:02d}.npz", **actor_item
            )
            actor_current = []
        if i % 30 == 0:
            print(f"Consistency and point generation {i + 1}/{len(views)}", flush=True)
    for frame, item in measured_frames.items():
        best = np.full(len(item["xyz"]), -np.inf)
        for i in range(frame * 6, frame * 6 + 6):
            uv, z = project(item["xyz"], known_k[i], known_poses[i])
            take = visible_rows(uv, z, (h, w))
            margin = np.minimum.reduce(
                (uv[take, 0], w - 1 - uv[take, 0], uv[take, 1], h - 1 - uv[take, 1])
            )
            better = margin > best[take]
            take, margin = take[better], margin[better]
            colours = sample_image(rgb[i], uv[take])
            good = np.isfinite(colours).all(axis=1)
            take, margin, colours = take[good], margin[good], colours[good]
            item["rgb"][take] = np.rint(colours).clip(0, 255).astype(np.uint8)
            best[take] = margin
    clouds, assets = {}, {}
    for method, item in (
        ("raw", combine(raw_items)),
        ("calibrated", combine(calibrated_items)),
        ("measured", combine(list(measured_frames.values()))),
    ):
        print(
            f"Consolidating full {method}: {len(item['xyz']):,} source pixels",
            flush=True,
        )
        clouds[method], assets[method] = export_cloud(root, method, item, origin)
    clouds["fused"], assets["fused"] = export_cloud(
        root, "fused", combine([clouds["measured"], clouds["calibrated"]]), origin
    )
    targets = fixed_targets(data, protocol, masks)
    truth = np.concatenate([t["truth_z"] for t in targets])
    scored = {name: [] for name in clouds}
    for target in targets:
        np.savez_compressed(
            root / f"targets-{target['frame']:02d}.npz",
            **{k: v for k, v in target.items() if k != "scan_token"},
        )
        for method, cloud in clouds.items():
            values = np.full(len(target["truth_z"]), np.nan)
            for i in np.unique(target["view"]):
                take = target["view"] == i
                values[take] = cloud_depth_at_targets(
                    cloud["xyz"],
                    target["uv"][take],
                    known_k[i],
                    known_poses[i],
                    (h, w),
                    radius=1.5,
                )
            scored[method].append(values)
        print(f"Scored globally heldout scan {target['frame']}", flush=True)
    scored = {k: np.concatenate(v) for k, v in scored.items()}
    np.savez_compressed(root / "sequence-scored-depths.npz", truth=truth, **scored)
    metrics = {k: error_summary(v, truth) for k, v in scored.items()}
    baseline, fused = scored["measured"], scored["fused"]
    base_correct = np.isfinite(baseline) & (np.abs(baseline - truth) <= 0.5)
    fused_correct = np.isfinite(fused) & (np.abs(fused - truth) <= 0.5)
    common = np.isfinite(baseline) & np.isfinite(fused)
    validation = {
        "targets": len(truth),
        "heldout_frames": protocol["heldout_frames"],
        "heldout_tokens": [
            protocol["scans"][f]["token"] for f in protocol["heldout_frames"]
        ],
        "metrics": metrics,
        "fused_new_correct": int((fused_correct & ~base_correct).sum()),
        "fused_lost_correct": int((base_correct & ~fused_correct).sum()),
        "common_targets": int(common.sum()),
        "common_measured": error_summary(baseline, truth, common),
        "common_fused": error_summary(fused, truth, common),
        "scope": "static camera-Z at fixed unique sparse LiDAR targets; not dense surface or unseen RGB accuracy",
    }
    write_json(root / "sequence-validation.json", validation)
    for frame in range(count):
        for cam, camera in enumerate(CAMERAS):
            i = frame * 6 + cam
            actor = np.load(root / "frames" / f"actors-{frame:02d}.npz")
            actor_item = {k: actor[k] for k in empty()}
            render = render_points(
                combine([clouds["calibrated"], actor_item]), views[i], (h, w)
            )
            Image.fromarray(render).save(
                root / "frames" / f"render-{i:03d}.jpg", quality=94
            )
        if frame % 5 == 0:
            print(f"Rendered six-camera RGB frame {frame + 1}/{count}", flush=True)
    pose_stats = {
        "alignment_scale": scale_pose,
        "alignment_rotation": rotation,
        "alignment_translation": translation,
        "fit_cameras": int(fit_rows.sum()),
        "camera_centres_rmse_m": float(np.sqrt(np.mean(pose_error**2))),
        "camera_centres_median_m": float(np.median(pose_error)),
        "camera_centres_p90_m": float(np.quantile(pose_error, 0.9)),
        "rotation_median_deg": float(np.median(rotation_error)),
        "rotation_p90_deg": float(np.quantile(rotation_error, 0.9)),
    }
    camera_records = []
    for i, view in enumerate(views):
        camera_records.append(
            {
                "index": i,
                "frame": view["frame"],
                "camera": view["camera"],
                "timestamp": view["timestamp"],
                "seconds": (view["timestamp"] - views[0]["timestamp"]) / 1e6,
                "rgb": f"rgb/{i:03d}.jpg",
                "render": f"frames/render-{i:03d}.jpg",
                "intrinsic": known_k[i],
                "raw_intrinsic": predicted_k[i],
                "viewer_from_camera_cv": viewer_pose(known_poses[i], origin),
                "raw_viewer_from_camera_cv": viewer_pose(aligned_poses[i], origin),
                "pose_error_m": float(pose_error[i]),
            }
        )
    xyz_viewer = (
        clouds["calibrated"]["xyz"].astype(float) - origin
    ) @ VIEWER_FROM_WORLD.T
    ui = (
        Path(__file__).resolve().parents[2]
        / "web"
        / "scripts"
        / "vggt-omega-sequence-ui"
    )
    for source in ui.iterdir():
        if source.is_file():
            shutil.copyfile(source, root / source.name)
    write_json(
        root / "sequence-data.json",
        {
            "scene": protocol["scene"],
            "frames": count,
            "input_images": len(views),
            "seconds": (views[-6]["timestamp"] - views[0]["timestamp"]) / 1e6,
            "checkpoint": "VGGT-Omega-1B-512",
            "joint_forward_calls": 1,
            "grid": [w, h],
            "inference_seconds": inference["seconds"],
            "peak_gpu_mib": inference["peak_reserved_mib"],
            "origin": origin,
            "viewer_from_world": VIEWER_FROM_WORLD,
            "bounds": np.quantile(xyz_viewer, [0.01, 0.99], axis=0),
            "methods": METHODS,
            "assets": assets,
            "actors": actor_frames,
            "cameras": camera_records,
            "pose": pose_stats,
            "scale": scale_stats,
            "validation": validation,
        },
    )
    write_json(
        root / "sequence-build.json",
        {
            "seconds": time.perf_counter() - started,
            "protocol_sha256": digest(root / "sequence-build-protocol.json"),
            "views": per_view,
            "raw_pose_alignment": pose_stats,
            "depth_scale": scale_stats,
            "source_pixels_raw": sum(v["raw_points"] for v in per_view),
            "source_pixels_calibrated": sum(v["accepted_points"] for v in per_view),
            "source_views_with_raw_points": sum(v["raw_points"] > 0 for v in per_view),
            "source_views_with_calibrated_points": sum(
                v["accepted_points"] > 0 for v in per_view
            ),
        },
    )
    print(
        json.dumps(
            {
                "completed": True,
                "counts": {k: v["points"] for k, v in assets.items()},
                "metrics": metrics,
                "seconds": time.perf_counter() - started,
            }
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prediction-run", type=Path)
    build(parser.parse_args())


if __name__ == "__main__":
    main()
