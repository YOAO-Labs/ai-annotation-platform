"""Correct, fuse, score and display the frozen VGGT-Omega depth windows."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import maximum_filter, minimum_filter
from scipy.spatial import cKDTree

from omega_densification import (
    acquisition_split,
    cloud_depth_at_targets,
    error_summary,
    lidar_evidence,
    local_correction,
    project,
    robust_scale,
    sample_image,
    transform,
    unproject,
    visible_rows,
    voxel_representatives,
)
from qualify_vggt_omega import PARAMETERS, SceneData, digest
from vggt_common import (
    VIEWER_FROM_REFERENCE,
    actor_ray_mask,
    cross_view_support,
    points_in_actors,
    write_json,
)

METHODS = {
    "single_lidar": "前一帧 LiDAR",
    "temporal_lidar": "多帧实测 LiDAR",
    "scale_model": "模型 · 仅全局尺度",
    "corrected_model": "模型 · 局部校正",
    "filtered_model": "模型 · 时间一致性筛选",
    "fused": "实测 + 筛选模型",
}


def actor_mask(data, scene, view):
    return actor_ray_mask(
        np.asarray(view["intrinsic"]),
        (560, 320),
        np.asarray(view["global_from_sensor"]),
        data.boxes(scene, view["timestamp"]),
        PARAMETERS["actor_padding_m"],
    )


def anchors_for_view(points, identities, k, pose, mask):
    uv, z = project(points, k, pose)
    take = visible_rows(uv, z, mask.shape)
    px = np.rint(uv[take]).astype(int)
    take = take[~mask[px[:, 1], px[:, 0]]]
    return {"uv": uv[take], "z": z[take], "identities": identities[take]}


def targets(data, run, view_masks, depth, rgb):
    points, row = data.scan(run["scene"], run["centre"])
    count = len(points)
    assignment = np.full(count, -1, dtype=np.int16)
    best_margin = np.full(count, -np.inf)
    target_uv = np.full((count, 2), np.nan)
    target_z = np.full(count, np.nan)
    ray_actor = np.zeros(count, bool)
    edge = np.zeros(count, bool)
    actor = points_in_actors(
        points,
        data.boxes(run["scene"], row["timestamp"]),
        PARAMETERS["actor_padding_m"],
    )
    per_view_count = 0
    for i, view in enumerate(run["views"]):
        if view["frame"] != run["centre"]:
            continue
        uv, z = project(
            points,
            np.asarray(view["intrinsic"]),
            np.asarray(view["global_from_sensor"]),
        )
        take = visible_rows(uv, z, (320, 560))
        per_view_count += len(take)
        margin = np.minimum.reduce(
            (uv[take, 0], 559 - uv[take, 0], uv[take, 1], 303 - uv[take, 1])
        )
        better = margin > best_margin[take]
        rows = take[better]
        assignment[rows], best_margin[rows] = i, margin[better]
        target_uv[rows], target_z[rows] = uv[rows], z[rows]
        px = np.rint(uv[rows]).astype(int)
        ray_actor[rows] = view_masks[i][px[:, 1], px[:, 0]]
        gray = rgb[i].astype(float).mean(axis=2) / 255
        edge_image = maximum_filter(gray, size=3) - minimum_filter(gray, size=3) > 0.15
        edge[rows] = edge_image[px[:, 1], px[:, 0]]
    keep = np.flatnonzero(assignment >= 0)
    return {
        "points": points[keep],
        "raw_rows": keep,
        "scan_token": row["token"],
        "view": assignment[keep],
        "uv": target_uv[keep],
        "truth_z": target_z[keep],
        "actor": actor[keep],
        "static": ~actor[keep] & ~ray_actor[keep],
        "edge": edge[keep],
        "per_view_return_count": per_view_count,
        "excluded_occluded_background_targets": int(
            (~actor[keep] & ray_actor[keep]).sum()
        ),
    }


def cloud(points, rgb, source, pixel, confidence, support, kind):
    return {
        "points": np.asarray(points, np.float32),
        "rgb": np.asarray(rgb, np.uint8),
        "source": np.asarray(source, np.int16),
        "pixel": np.asarray(pixel, np.int32),
        "confidence": np.asarray(confidence, np.float32),
        "support": np.asarray(support, np.uint8),
        "kind": np.asarray(kind, np.uint8),
    }


def merge_clouds(items):
    if not items:
        return cloud(np.empty((0, 3)), np.empty((0, 3)), [], [], [], [], [])
    return {key: np.concatenate([item[key] for item in items]) for key in items[0]}


def consolidate(item):
    take = voxel_representatives(item["points"], PARAMETERS["voxel_m"])
    return {key: value[take] for key, value in item.items()}


def from_depth(z, rgb, view, view_index, selected, confidence, support, dynamic):
    y, x = np.nonzero(selected)
    xyz = unproject(
        np.column_stack((x, y)),
        z[y, x],
        np.asarray(view["intrinsic"]),
        np.asarray(view["global_from_sensor"]),
    )
    return cloud(
        xyz,
        rgb[y, x],
        np.full(len(x), view_index),
        y * 560 + x,
        confidence[y, x],
        support[y, x],
        np.where(dynamic[y, x], 2, 1),
    )


def measured_cloud(data, run, anchor_scans, single=False):
    pieces = []
    for index, scan in enumerate(run["scans"]):
        if scan["token"] not in anchor_scans or (
            single and scan["token"] != run["single_scan"]
        ):
            continue
        xyz, row = data.scan(run["scene"], scan["frame"])
        actor = points_in_actors(
            xyz,
            data.boxes(run["scene"], row["timestamp"]),
            PARAMETERS["actor_padding_m"],
        )
        reference = transform(xyz, np.asarray(run["reference_from_global"]))
        inside = np.linalg.norm(reference, axis=1) <= 80
        # The dynamic baseline is only the immediately preceding scan, without motion compensation.
        inside &= ~actor | (scan["token"] == run["single_scan"])
        raw_rows = np.flatnonzero(inside)
        rgb = np.broadcast_to(
            np.array([73, 200, 236], np.uint8), (len(raw_rows), 3)
        ).copy()
        rgb[actor[inside]] = [244, 167, 76]
        pieces.append(
            cloud(
                xyz[inside],
                rgb,
                np.full(len(raw_rows), -index - 1),
                raw_rows,
                np.ones(len(raw_rows)),
                np.zeros(len(raw_rows)),
                np.where(actor[inside], 2, 0),
            )
        )
    return consolidate(merge_clouds(pieces))


def save_cloud(directory, method, item, run):
    reference = transform(item["points"], np.asarray(run["reference_from_global"]))
    viewer = reference @ VIEWER_FROM_REFERENCE.T
    dtype = np.dtype([("xyz", "<f4", 3), ("rgb", "u1", 3), ("padding", "u1")])
    buffer = np.zeros(len(viewer), dtype=dtype)
    buffer["xyz"], buffer["rgb"] = viewer, item["rgb"]
    buffer.tofile(directory / f"{method}.bin")
    np.savez_compressed(
        directory / f"{method}-provenance.npz",
        **{k: v for k, v in item.items() if k not in ("points", "rgb")},
    )
    # Viewer receives kind as a compact separate attribute; it uses original RGB by default.
    item["kind"].tofile(directory / f"{method}-kind.bin")
    counts = {
        "total": len(item["points"]),
        "measured_static": int((item["kind"] == 0).sum()),
        "generated_static": int((item["kind"] == 1).sum()),
        "actor_diagnostic": int((item["kind"] == 2).sum()),
    }
    return {
        "url": f"runs/{run['id']}/{method}.bin",
        "kind_url": f"runs/{run['id']}/{method}-kind.bin",
        "counts": counts,
    }


def score_cloud(item, target, run):
    predictions = np.full(len(target["truth_z"]), np.nan)
    for i, view in enumerate(run["views"]):
        selected = target["view"] == i
        if not selected.any():
            continue
        for actors in (False, True):
            group = selected & (target["actor"] if actors else target["static"])
            points = (
                item["points"][item["kind"] == 2]
                if actors
                else item["points"][item["kind"] != 2]
            )
            predictions[group] = cloud_depth_at_targets(
                points,
                target["uv"][group],
                np.asarray(view["intrinsic"]),
                np.asarray(view["global_from_sensor"]),
                (320, 560),
                PARAMETERS["evaluation_pixel_radius"],
            )
    static = target["static"]
    summary = {
        "static": error_summary(predictions, target["truth_z"], static),
        "actors_diagnostic": error_summary(
            predictions, target["truth_z"], target["actor"]
        ),
    }
    for name, mask in (
        ("near_lt20m", target["truth_z"] < 20),
        ("mid_20to40m", (target["truth_z"] >= 20) & (target["truth_z"] < 40)),
        ("far_40to80m", target["truth_z"] >= 40),
        ("edge", target["edge"]),
        ("interior", ~target["edge"]),
    ):
        summary[name] = error_summary(predictions, target["truth_z"], static & mask)
    if static.any() and (item["kind"] != 2).any():
        distances = cKDTree(item["points"][item["kind"] != 2]).query(
            target["points"][static], workers=8
        )[0]
        summary["sparse_holdout_3d_proximity_proxy"] = error_summary(
            distances, np.zeros(len(distances))
        )
    return predictions, summary


def overlay(directory, target, views, rgb, predictions):
    result = []
    for i, view in enumerate(views):
        if view["frame"] != directory[1]:
            continue
        name = view["camera"].lower().replace("_", "-")
        image = Image.fromarray(rgb[i])
        image.save(directory[0] / f"{name}.jpg", quality=94)
        painted = image.copy()
        draw = ImageDraw.Draw(painted)
        take = np.flatnonzero((target["view"] == i) & target["static"])
        for row in take:
            x, y = target["uv"][row]
            predicted = predictions[row]
            colour = (
                (140, 149, 162)
                if not np.isfinite(predicted)
                else (
                    (66, 225, 163)
                    if abs(predicted - target["truth_z"][row]) <= 0.5
                    else (255, 89, 102)
                )
            )
            draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill=colour)
        painted.save(directory[0] / f"{name}-holdout.jpg", quality=94)
        result.append(
            {
                "camera": view["camera"],
                "rgb": f"{name}.jpg",
                "overlay": f"{name}-holdout.jpg",
            }
        )
    return result


def compare_run(output, data, run):
    started = time.perf_counter()
    directory = output / "runs" / run["id"]
    directory.mkdir(parents=True, exist_ok=True)
    with np.load(output / "predictions" / f"{run['id']}.npz") as saved:
        depth, confidence, rgb = (
            saved["depth"].copy(),
            saved["confidence"].copy(),
            saved["rgb"].copy(),
        )
    views = run["views"]
    masks = [actor_mask(data, run["scene"], view) for view in views]
    allowed = acquisition_split([s["token"] for s in run["scans"]], run["target_scan"])
    if allowed != run["anchor_scans"]:
        raise ValueError("calibration acquisition boundary changed")
    xyz_parts, id_parts = [], []
    scan_diagnostics = []
    for scan in run["scans"]:
        if scan["token"] not in allowed:
            continue
        xyz, row = data.scan(run["scene"], scan["frame"])
        if digest(scan["path"]) != scan["sha256"]:
            raise ValueError("LiDAR acquisition changed")
        boxes = data.boxes(run["scene"], row["timestamp"])
        static = ~points_in_actors(xyz, boxes, PARAMETERS["actor_padding_m"])
        rows = np.flatnonzero(static)
        xyz_parts.append(xyz[rows])
        id_parts.append(np.array([f"{row['token']}:{r}" for r in rows]))
        scan_diagnostics.append(
            {
                "token": row["token"],
                "frame": scan["frame"],
                "raw_returns": len(xyz),
                "static_anchors": int(static.sum()),
                "clamped_boxes": sum(b["clamped_timestamp"] for b in boxes),
            }
        )
    anchor_points, identities = np.concatenate(xyz_parts), np.concatenate(id_parts)
    anchors, predicted, measured, ids = [], [], [], []
    for i, view in enumerate(views):
        a = anchors_for_view(
            anchor_points,
            identities,
            np.asarray(view["intrinsic"]),
            np.asarray(view["global_from_sensor"]),
            masks[i],
        )
        anchors.append(a)
        predicted.append(sample_image(depth[i], a["uv"]))
        measured.append(a["z"])
        ids.append(a["identities"])
    scale, scale_stats = robust_scale(
        np.concatenate(predicted), np.concatenate(measured), np.concatenate(ids)
    )
    scaled = depth * scale
    corrected, locally_supported, correction_stats = [], [], []
    valid_masks = []
    for i, view in enumerate(views):
        a = anchors[i]
        corrected_i, local, stats = local_correction(
            scaled[i],
            rgb[i],
            a["uv"],
            a["z"],
            PARAMETERS["local_radius_px"],
            PARAMETERS["local_neighbors"],
        )
        cutoff = np.quantile(confidence[i], PARAMETERS["confidence_quantile"])
        valid = (
            np.isfinite(corrected_i)
            & (corrected_i >= 1)
            & (corrected_i <= 80)
            & (confidence[i] >= cutoff)
        )
        valid[304:] = False
        corrected.append(corrected_i)
        locally_supported.append(local)
        valid_masks.append(valid)
        correction_stats.append(
            {
                "token": view["token"],
                "camera": view["camera"],
                "frame": view["frame"],
                "confidence_cutoff": float(cutoff),
                "actor_mask_pixels": int(masks[i].sum()),
                "clamped_actor_boxes": sum(
                    b["clamped_timestamp"]
                    for b in data.boxes(run["scene"], view["timestamp"])
                ),
                **stats,
            }
        )
    corrected = np.asarray(corrected)
    target = targets(data, run, masks, scaled, rgb)
    methods = {
        "single_lidar": measured_cloud(data, run, allowed, single=True),
        "temporal_lidar": measured_cloud(data, run, allowed),
    }
    scale_pieces, corrected_pieces, filtered_pieces = [], [], []
    support_stats = []
    for i, view in enumerate(views):
        static_valid = valid_masks[i] & ~masks[i]
        source_depth = np.where(static_valid, corrected[i], np.nan)
        refs = [
            {
                "reference_from_camera": np.asarray(v["global_from_sensor"]),
                "intrinsic": np.asarray(v["intrinsic"]),
                "model": np.where(valid_masks[j] & ~masks[j], corrected[j], np.nan),
            }
            for j, v in enumerate(views)
            if v["camera"] == view["camera"] and v["frame"] != view["frame"]
        ]
        support, conflict = cross_view_support(
            source_depth,
            np.asarray(view["intrinsic"]),
            np.asarray(view["global_from_sensor"]),
            refs,
            PARAMETERS["support_absolute_m"],
            PARAMETERS["support_relative"],
            PARAMETERS["support_cycle_px"],
        )
        # A direct measured anchor is a bounded exception to temporal support.
        a = anchors[i]
        direct, lidar_conflict = lidar_evidence(
            corrected[i],
            a["uv"],
            a["z"],
            PARAMETERS["anchor_absolute_m"],
            PARAMETERS["anchor_relative"],
        )
        filtered = (
            static_valid & ((support >= 1) | direct) & (conflict == 0) & ~lidar_conflict
        )
        # Dynamic geometry is diagnostic, reconstructed only at centre RGB exposure times.
        dynamic = valid_masks[i] & masks[i] & (view["frame"] == run["centre"])
        zero = np.zeros((320, 560), np.uint8)
        raw_valid = (
            np.isfinite(scaled[i])
            & (scaled[i] >= 1)
            & (scaled[i] <= 80)
            & (confidence[i] >= correction_stats[i]["confidence_cutoff"])
        )
        raw_valid[304:] = False
        raw_valid &= ~masks[i] | (view["frame"] == run["centre"])
        scale_pieces.append(
            from_depth(
                scaled[i], rgb[i], view, i, raw_valid, confidence[i], zero, masks[i]
            )
        )
        corrected_pieces.append(
            from_depth(
                corrected[i],
                rgb[i],
                view,
                i,
                static_valid | dynamic,
                confidence[i],
                zero,
                masks[i],
            )
        )
        filtered_pieces.append(
            from_depth(
                corrected[i],
                rgb[i],
                view,
                i,
                filtered | dynamic,
                confidence[i],
                support,
                masks[i],
            )
        )
        support_stats.append(
            {
                "token": view["token"],
                "eligible_static": int(static_valid.sum()),
                "accepted_static": int(filtered.sum()),
                "temporal_supported": int(
                    (static_valid & (support >= 1) & (conflict == 0)).sum()
                ),
                "direct_anchor_only": int((filtered & (support == 0)).sum()),
                "visible_conflicts": int((static_valid & (conflict > 0)).sum()),
                "lidar_free_space_conflicts": int(
                    (static_valid & lidar_conflict).sum()
                ),
                "temporally_supported_but_lidar_rejected": int(
                    (static_valid & (support >= 1) & lidar_conflict).sum()
                ),
            }
        )
    methods["scale_model"] = consolidate(merge_clouds(scale_pieces))
    methods["corrected_model"] = consolidate(merge_clouds(corrected_pieces))
    methods["filtered_model"] = consolidate(merge_clouds(filtered_pieces))
    # Measurements take priority when they share a voxel with a generated sample.
    measured = methods["temporal_lidar"]
    measured_static = {k: v[measured["kind"] == 0] for k, v in measured.items()}
    methods["fused"] = consolidate(
        merge_clouds([measured_static, methods["filtered_model"]])
    )
    target_npz = {k: v for k, v in target.items() if isinstance(v, np.ndarray)}
    np.savez_compressed(directory / "targets.npz", **target_npz)
    predictions, metrics, assets = {}, {}, {}
    for method, item in methods.items():
        predictions[method], metrics[method] = score_cloud(item, target, run)
        assets[method] = save_cloud(directory, method, item, run)
    common = target["static"].copy()
    for prediction in predictions.values():
        common &= np.isfinite(prediction)
    baseline = predictions["temporal_lidar"]
    truth = target["truth_z"]
    for method, prediction in predictions.items():
        metrics[method]["common_all_methods_static"] = error_summary(
            prediction, truth, common
        )
        paired = target["static"] & np.isfinite(baseline) & np.isfinite(prediction)
        metrics[method]["common_temporal_lidar_static"] = error_summary(
            prediction, truth, paired
        )
        additions = {}
        for tolerance in PARAMETERS["evaluation_tolerances_m"]:
            good = np.isfinite(prediction) & (np.abs(prediction - truth) <= tolerance)
            baseline_good = np.isfinite(baseline) & (
                np.abs(baseline - truth) <= tolerance
            )
            n = int(target["static"].sum())
            additions[f"{tolerance:g}m"] = {
                "new_correct_targets": int(
                    (target["static"] & good & ~baseline_good).sum()
                ),
                "lost_correct_targets": int(
                    (target["static"] & ~good & baseline_good).sum()
                ),
                "new_correct_all_target_fraction": float(
                    (target["static"] & good & ~baseline_good).sum() / n
                ),
                "lost_correct_all_target_fraction": float(
                    (target["static"] & ~good & baseline_good).sum() / n
                ),
            }
        metrics[method]["relative_to_temporal_lidar"] = additions
    np.savez_compressed(directory / "scored-depths.npz", **predictions)
    np.savez_compressed(
        directory / "corrected-depths.npz",
        scaled=scaled,
        corrected=corrected,
        local_supported=np.asarray(locally_supported),
        valid=np.asarray(valid_masks),
    )
    measured_voxels = set(
        map(
            tuple,
            np.floor(
                methods["temporal_lidar"]["points"][
                    methods["temporal_lidar"]["kind"] == 0
                ]
                / PARAMETERS["voxel_m"]
            ).astype(int),
        )
    )
    candidate_voxels = set(
        map(
            tuple,
            np.floor(
                methods["filtered_model"]["points"][
                    methods["filtered_model"]["kind"] == 1
                ]
                / PARAMETERS["voxel_m"]
            ).astype(int),
        )
    )
    output_actor_diagnostics = {}
    current_boxes = data.boxes(
        run["scene"],
        next(s["timestamp"] for s in run["scans"] if s["token"] == run["target_scan"]),
    )
    for name in ("scale_model", "corrected_model", "filtered_model", "fused"):
        item = methods[name]
        dynamic = item["kind"] == 2
        source_indices = item["source"][dynamic]
        if (source_indices < 0).any():
            raise ValueError("generated actor diagnostic contains another LiDAR time")
        if any(
            views[int(i)]["frame"] != run["centre"] for i in np.unique(source_indices)
        ):
            raise ValueError(
                "temporal actor fusion is prohibited without motion compensation"
            )
        static_points = item["points"][item["kind"] != 2]
        overlaps = points_in_actors(static_points, current_boxes, 0)
        output_actor_diagnostics[name] = {
            "noncentre_actor_samples": 0,
            "static_points_in_centre_actor_boxes": int(overlaps.sum()),
            "static_points_in_centre_actor_fraction": float(overlaps.mean())
            if len(overlaps)
            else None,
            "interpretation": "oracle ray masks; residual overlap is diagnostic, not a complete trail metric",
        }
    original_xyz, _ = data.scan(run["scene"], run["centre"])
    oracle = cloud(
        original_xyz,
        np.tile([255, 171, 66], (len(original_xyz), 1)),
        np.full(len(original_xyz), -100),
        np.arange(len(original_xyz)),
        np.ones(len(original_xyz)),
        np.zeros(len(original_xyz)),
        np.full(len(original_xyz), 3),
    )
    assets["heldout_oracle"] = save_cloud(directory, "heldout_oracle", oracle, run)
    images = overlay(
        (directory, run["centre"]), target, views, rgb, predictions["fused"]
    )
    deltas = [
        v["timestamp"]
        - next(s["timestamp"] for s in run["scans"] if s["token"] == run["target_scan"])
        for v in views
        if v["frame"] == run["centre"]
    ]
    record = {
        "id": run["id"],
        "scene": run["scene"],
        "centre": run["centre"],
        "window": run["window"],
        "split": run["split"],
        "metrics": metrics,
        "clouds": assets,
        "images": images,
        "scale_fit": scale_stats,
        "correction": correction_stats,
        "support_filter": support_stats,
        "scans": scan_diagnostics,
        "unique_visible_holdout_returns": len(target["points"]),
        "static_holdout_returns": int(target["static"].sum()),
        "actor_holdout_returns": int(target["actor"].sum()),
        "view_return_observations": target["per_view_return_count"],
        "excluded_occluded_background_targets": target[
            "excluded_occluded_background_targets"
        ],
        "centre_rgb_minus_lidar_us": deltas,
        "new_static_voxels": len(candidate_voxels - measured_voxels),
        "actor_source_audit": output_actor_diagnostics,
        "seconds_postprocess": time.perf_counter() - started,
    }
    write_json(directory / "metrics.json", record)
    print(
        json.dumps(
            {
                "id": run["id"],
                "scale": scale,
                "points": assets["fused"]["counts"],
                "lidar_correct_0.5m": metrics["temporal_lidar"]["static"][
                    "correct_coverage_0.5m"
                ],
                "fused_correct_0.5m": metrics["fused"]["static"][
                    "correct_coverage_0.5m"
                ],
                "seconds": record["seconds_postprocess"],
            }
        ),
        flush=True,
    )
    return record


def compare(args):
    output = args.output
    protocol = json.loads((output / "protocol.json").read_text())
    if protocol["parameters"] != PARAMETERS:
        raise ValueError("frozen parameters changed")
    data = SceneData(Path(protocol["data_root"]))
    (output / "runs").mkdir(exist_ok=True)
    records = []
    inference = json.loads((output / "inference.json").read_text())
    expected_predictions = {
        r["id"]: r["prediction_sha256"]
        for r in inference["runs"]
        if r["status"] == "completed"
    }
    for run in protocol["runs"]:
        if not (output / "predictions" / f"{run['id']}.npz").exists():
            raise ValueError(f"missing actual inference: {run['id']}")
        if digest(
            output / "predictions" / f"{run['id']}.npz"
        ) != expected_predictions.get(run["id"]):
            raise ValueError(f"prediction changed after inference: {run['id']}")
        records.append(compare_run(output, data, run))
    summary = {}
    for split in ("development", "validation", "all"):
        selected = [r for r in records if split == "all" or r["split"] == split]
        summary[split] = {}
        for window in (3, 5):
            rows = [r for r in selected if r["window"] == window]
            summary[split][str(window)] = {}
            for method in METHODS:
                truths, pred, masks = [], [], []
                for row in rows:
                    directory = output / "runs" / row["id"]
                    with np.load(directory / "targets.npz") as saved:
                        truths.append(saved["truth_z"].copy())
                        masks.append(saved["static"].copy())
                    with np.load(directory / "scored-depths.npz") as saved:
                        pred.append(saved[method].copy())
                summary[split][str(window)][method] = error_summary(
                    np.concatenate(pred), np.concatenate(truths), np.concatenate(masks)
                )
    inference = json.loads((output / "inference.json").read_text())
    write_json(
        output / "data.json",
        {
            "title": "VGGT-Ω · LiDAR 约束点云增密",
            "methods": METHODS,
            "runs": records,
            "parameters": PARAMETERS,
            "summary": summary,
            "inference": inference,
        },
    )
    write_json(output / "summary.json", summary)
    ui = Path(__file__).resolve().parents[2] / "web/scripts/vggt-omega-review-ui"
    for path in ui.iterdir():
        if path.is_file():
            shutil.copy2(path, output / path.name)
    three = Path(__file__).resolve().parents[2] / "web/node_modules/three"
    (output / "vendor").mkdir(exist_ok=True)
    for source, name in (
        (three / "build/three.module.js", "three.module.js"),
        (three / "build/three.core.js", "three.core.js"),
        (three / "examples/jsm/controls/OrbitControls.js", "OrbitControls.js"),
    ):
        shutil.copy2(source, output / "vendor" / name)
    write_json(
        output / "implementation.json",
        {
            "source_hashes": {
                str(p): digest(p)
                for p in (
                    Path(__file__),
                    Path(__file__).with_name("omega_densification.py"),
                    Path(__file__).with_name("qualify_vggt_omega.py"),
                )
            },
            "protocol_sha256": digest(output / "protocol.json"),
            "prediction_sha256_verified": expected_predictions,
            "freeze_source_hashes_describe_initial_scaffold": True,
            "postprocess_fix": "veto sparse LiDAR free-space conflicts using the originally frozen anchor tolerance",
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    compare(parser.parse_args())
