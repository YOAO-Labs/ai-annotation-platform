"""Independent provenance, leave-out and rigid-motion audit of saved artifacts."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.spatial import cKDTree

from omega_densification import (
    cloud_depth_at_targets,
    error_summary,
    project,
    transform,
    unproject,
    visible_rows,
)
from omega_dynamic import (
    ActorTracks,
    assign_instances,
    overlapping_windows,
    warp_instance,
)
from qualify_vggt_omega import CAMERAS, SceneData, digest
from vggt_common import verify_runtime_sources, write_json


def audit(root):
    started = time.perf_counter()
    protocol = json.loads((root / "sweeps-protocol.json").read_text())
    inference = json.loads((root / "sweeps-inference.json").read_text())
    build_protocol = json.loads((root / "sweeps-build-protocol.json").read_text())
    build = json.loads((root / "sweeps-build.json").read_text())
    display = json.loads((root / "sweeps-data.json").read_text())
    validation = json.loads((root / "dynamic-validation.json").read_text())
    if inference["status"] != "completed" or inference["forward_calls"] != 18:
        raise ValueError("not eighteen completed window forwards")
    if (
        inference["protocol_sha256"] != digest(root / "sweeps-protocol.json")
        or build_protocol["protocol_sha256"] != inference["protocol_sha256"]
    ):
        raise ValueError("protocol provenance disconnected")
    if build_protocol["inference_sha256"] != digest(root / "sweeps-inference.json"):
        raise ValueError("inference record changed")
    for folder, hashes in (
        ("inference-source", protocol["source_hashes"]),
        ("build-source", build_protocol["source_hashes"]),
    ):
        for name, expected in hashes.items():
            if digest(root / folder / name) != expected:
                raise ValueError("executed source snapshot changed")
    views, scans = protocol["views"], protocol["scans"]
    if (
        len(views) != 1318
        or len({v["token"] for v in views}) != 1318
        or len({v["path"] for v in views}) != 1318
    ):
        raise ValueError("RGB inputs are missing or duplicated")
    windows, owner = overlapping_windows(1318)
    if windows != protocol["windows"] or not np.array_equal(
        owner, [v["owner_window"] for v in views]
    ):
        raise ValueError("output ownership differs from frozen rule")
    if not np.all(np.diff([v["timestamp"] for v in views]) > 0):
        raise ValueError("exposures are not ordered by distinct real timestamps")
    heldout = set(protocol["heldout_tokens"])
    if len(heldout) != 11 or len(scans) != 382:
        raise ValueError("unexpected acquisition split")
    target_scans = [s for s in scans if s["key_frame"] in (8, 19, 30)]
    if len(target_scans) != 3:
        raise ValueError("expected three heldout key acquisition timestamps")
    recomputed = {
        s["token"]
        for s in scans
        if any(
            abs(s["timestamp"] - target["timestamp"]) <= 100000
            for target in target_scans
        )
    }
    if recomputed != heldout or any(
        s["heldout"] != (s["token"] in recomputed) for s in scans
    ):
        raise ValueError("heldout labels disagree with the independent time exclusion")
    for row in views + scans:
        if digest(row["path"]) != row["sha256"]:
            raise ValueError("frozen sensor file changed")
    for record in build["views"]:
        for branch, calibration in record["calibration"].items():
            if calibration["anchor_tokens"] != [
                scans[i]["token"] for i in calibration["anchor_indices"]
            ]:
                raise ValueError("anchor identity and index mappings disagree")
            if (
                heldout.intersection(calibration["anchor_tokens"])
                or len(calibration["anchor_tokens"]) > 4
            ):
                raise ValueError("calibration leaked heldout data or exceeded cap")
            for index in calibration["anchor_indices"]:
                if scans[index]["heldout"]:
                    raise ValueError("heldout scan index used for calibration")
                if (
                    abs(scans[index]["timestamp"] - views[record["index"]]["timestamp"])
                    > 1000000
                ):
                    raise ValueError("anchor exceeds frozen time distance")
                if branch == "key" and not scans[index]["is_key_frame"]:
                    raise ValueError("non-key anchor leaked into key-only control")
    scans_by_token = {s["token"]: s for s in scans}
    for view, objects in zip(views, build["actor_candidates"]):
        for actor in objects:
            if heldout.intersection(actor["anchor_tokens"]):
                raise ValueError("dynamic object calibration leaked heldout data")
            if any(
                abs(scans_by_token[token]["timestamp"] - view["timestamp"]) > 150000
                for token in actor["anchor_tokens"]
            ):
                raise ValueError(
                    "dynamic anchor exceeds its actual source exposure time bound"
                )
    for record in inference["windows"]:
        if (
            digest(root / record["path"]) != record["sha256"]
            or record["indices"] != windows[record["index"]]
        ):
            raise ValueError("prediction changed or wrong window inputs")
    data = SceneData(Path(protocol["data_root"]))
    for name, expected in protocol["metadata_sha256"].items():
        if digest(Path(protocol["data_root"]) / "v1.0-mini" / name) != expected:
            raise ValueError("metadata changed")
    tracks = ActorTracks(data.tracks[protocol["scene"]])
    basis = np.array([[1.0, 0, 0], [0, 0, 1], [0, -1, 0]])
    origin = np.asarray(views[0]["global_from_sensor"])[:3, 3].copy()
    origin[2] = (
        np.median([np.asarray(v["global_from_sensor"])[2, 3] for v in views]) - 1.5
    )
    random = np.random.default_rng(1318)
    clouds, samples = {}, {}
    requests = {}
    maximum_xyz = 0.0
    sampled = 0
    scan_cache = {}
    for method, asset in display["assets"].items():
        if digest(root / asset["full_file"]) != asset["full_sha256"]:
            raise ValueError("saved full cloud changed")
        cloud = dict(np.load(root / asset["full_file"]))
        clouds[method] = cloud
        size = len(cloud["xyz"])
        if (
            size != asset["points"]
            or not np.isfinite(cloud["xyz"]).all()
            or any(len(a) != size for a in cloud.values())
        ):
            raise ValueError("invalid cloud shape or coordinates")
        if (cloud["source"] >= 1318).any() or (cloud["source"] < -382).any():
            raise ValueError("source identity out of bounds")
        measured_ids = np.unique(-cloud["source"][cloud["source"] < 0] - 1)
        if any(scans[int(i)]["token"] in heldout for i in measured_ids):
            raise ValueError("heldout scan found in output cloud")
        if method.endswith("key") and any(
            not scans[int(i)]["is_key_frame"] for i in measured_ids
        ):
            raise ValueError("non-key measured point in key-only control")
        if not np.array_equal(cloud["kind"] == 0, cloud["source"] < 0):
            raise ValueError("provenance kind mismatch")
        take = random.choice(size, min(size, 14000), replace=False)
        samples[method] = take
        for source in np.unique(cloud["source"][take]):
            selected = take[cloud["source"][take] == source]
            if source < 0:
                index = -int(source) - 1
                if index not in scan_cache:
                    raw = np.fromfile(scans[index]["path"], dtype="<f4").reshape(-1, 5)[
                        :, :3
                    ]
                    scan_cache[index] = transform(
                        raw, np.asarray(scans[index]["global_from_sensor"])
                    )
                expected = scan_cache[index][cloud["pixel"][selected]]
                maximum_xyz = max(
                    maximum_xyz,
                    float(
                        np.linalg.norm(cloud["xyz"][selected] - expected, axis=1).max(
                            initial=0
                        )
                    ),
                )
                sampled += len(selected)
            else:
                branch = (
                    "raw"
                    if method == "raw"
                    else ("all" if method.endswith("all") else "key")
                )
                requests.setdefault((branch, int(source)), []).append(
                    (method, selected)
                )
        for budget, lod in asset["lod"].items():
            path = root / lod["path"]
            if (
                digest(path) != lod["sha256"]
                or path.stat().st_size != lod["points"] * 16
            ):
                raise ValueError("display point buffer changed")
            indices = np.load(root / f"{method}-{budget}-indices.npy")
            packed = np.fromfile(
                path, dtype=[("xyz", "<f4", (3,)), ("rgb", "u1", (3,)), ("kind", "u1")]
            )
            expected = (cloud["xyz"][indices].astype(float) - origin) @ basis.T
            if (
                not np.allclose(packed["xyz"], expected, atol=3e-5, rtol=0)
                or not np.array_equal(packed["rgb"], cloud["rgb"][indices])
                or not np.array_equal(packed["kind"], cloud["kind"][indices])
            ):
                raise ValueError("display differs from saved source samples")
        print(f"Validated cloud and display {method}: {size:,}", flush=True)
    prediction_cache = {}
    rgb_cache = {}

    def source_rgb(index):
        if index not in rgb_cache:
            with Image.open(views[index]["path"]) as image:
                rgb_cache[index] = np.asarray(
                    image.convert("RGB").resize((640, 352), Image.Resampling.BICUBIC)
                )
        return rgb_cache[index]

    for (branch, i), records in requests.items():
        view = views[i]
        rgb = source_rgb(i)
        if branch == "raw":
            own = view["owner_window"]
            if own not in prediction_cache:
                prediction_cache.clear()
                prediction_cache[own] = dict(
                    np.load(root / inference["windows"][own]["path"])
                )
            arrays = prediction_cache[own]
            local = windows[own].index(i)
            k = arrays["intrinsic"][local].astype(float)
            k[:2, 2] -= 0.5
            ex = arrays["extrinsic"][local].astype(float)
            pose = np.eye(4)
            pose[:3, :3] = ex[:, :3].T
            pose[:3, 3] = -pose[:3, :3] @ ex[:, 3]
            stats = build["windows"][own]
            rot = np.asarray(stats["rotation"])
            scale = stats["native_sim3_scale"]
            pose[:3, :3] = rot @ pose[:3, :3]
            pose[:3, 3] = rot @ pose[:3, 3] * scale + stats["translation"]
            depth = arrays["depth"][local] * scale
            accepted = None
        else:
            saved = np.load(root / "views" / f"{branch}-{i:04d}.npz")
            depth, accepted = saved["corrected"], saved["accepted"]
            k, pose = (
                np.asarray(view["intrinsic"]),
                np.asarray(view["global_from_sensor"]),
            )
        for method, selected in records:
            cloud = clouds[method]
            pixels = cloud["pixel"][selected]
            uv = np.column_stack((pixels % 640, pixels // 640))
            if accepted is not None and not accepted.flat[pixels].all():
                raise ValueError("saved sample was not accepted by its source view")
            expected = unproject(uv, depth.flat[pixels], k, pose)
            maximum_xyz = max(
                maximum_xyz,
                float(
                    np.linalg.norm(cloud["xyz"][selected] - expected, axis=1).max(
                        initial=0
                    )
                ),
            )
            if not np.array_equal(cloud["rgb"][selected], rgb.reshape(-1, 3)[pixels]):
                raise ValueError("point colour differs from actual input exposure")
            sampled += len(selected)
    if maximum_xyz > 0.0002:
        raise ValueError(f"source geometry reconstruction mismatch: {maximum_xyz}")
    old = dict(np.load(root.parent / "full-sequence" / "measured-full.npz"))
    control_equal = np.array_equal(old["xyz"], clouds["measured_key"]["xyz"])
    if not control_equal:
        raise ValueError(
            "key-only measured geometry differs from previous fixed baseline"
        )
    for branch in ("key", "all"):
        fused, measured = clouds[f"fused_{branch}"], clouds[f"measured_{branch}"]
        real = fused["source"] < 0
        if any(not np.array_equal(fused[key][real], measured[key]) for key in measured):
            raise ValueError("fusion modified or removed an existing measured sample")
    scores = np.load(root / "static-scored-depths.npz")
    frozen_truth = np.concatenate(
        [np.load(t["path"])["truth_z"] for t in protocol["fixed_static_targets"]]
    )
    if not np.array_equal(scores["truth"], frozen_truth):
        raise ValueError("scored static truth differs from frozen targets")
    static_validation = json.loads((root / "static-validation.json").read_text())
    for method in clouds:
        actual = error_summary(scores[method], frozen_truth)
        if (
            actual != static_validation["metrics"][method]
            or actual != display["static"]["metrics"][method]
        ):
            raise ValueError("reported static metrics differ from saved target scores")
    checked_targets = 0
    offset = 0
    projection_loss = []
    for frozen in protocol["fixed_static_targets"]:
        if digest(frozen["path"]) != frozen["sha256"]:
            raise ValueError("static target changed")
        target = np.load(frozen["path"])
        old_view = min(np.unique(target["view"]))
        rows = np.flatnonzero(target["view"] == old_view)
        take = random.choice(rows, min(80, len(rows)), replace=False)
        i = int(frozen["view_mapping"][str(old_view)])
        for method, cloud in clouds.items():
            fresh = cloud_depth_at_targets(
                cloud["xyz"],
                target["uv"][take],
                np.asarray(views[i]["intrinsic"]),
                np.asarray(views[i]["global_from_sensor"]),
                (352, 640),
                1.5,
            )
            if not np.allclose(
                fresh, scores[method][offset + take], atol=1e-9, rtol=0, equal_nan=True
            ):
                raise ValueError(
                    "static scores differ from actual saved cloud reprojection"
                )
            checked_targets += len(take)
        # Diagnose negative fusion results without using the targets to tune the method.
        global_rows = offset + rows
        base, fused = (
            scores["measured_all"][global_rows],
            scores["fused_all"][global_rows],
        )
        lost = (
            np.isfinite(base)
            & (np.abs(base - target["truth_z"][rows]) <= 0.5)
            & np.isfinite(fused)
            & (np.abs(fused - target["truth_z"][rows]) > 0.5)
        )
        queries = rows[lost]
        if len(queries):
            cloud = clouds["fused_all"]
            uv, z = project(
                cloud["xyz"],
                np.asarray(views[i]["intrinsic"]),
                np.asarray(views[i]["global_from_sensor"]),
            )
            visible = visible_rows(uv, z, (352, 640))
            distance, near = cKDTree(uv[visible]).query(
                target["uv"][queries], k=1, distance_upper_bound=1.5
            )
            if not np.isfinite(distance).all():
                raise ValueError("diagnostic target lost the saved fused support")
            winners = visible[near]
            if not np.allclose(
                z[winners], scores["fused_all"][offset + queries], atol=1e-9, rtol=0
            ):
                raise ValueError(
                    "diagnostic projection winner differs from saved score"
                )
            chosen = np.linspace(0, len(queries) - 1, min(30, len(queries)), dtype=int)
            projection_loss.append(
                {
                    "frame": frozen["frame"],
                    "source_camera": views[i]["camera"],
                    "lost_targets_in_camera": len(queries),
                    "winning_model_points": int((cloud["kind"][winners] == 1).sum()),
                    "winning_measured_points": int((cloud["kind"][winners] == 0).sum()),
                    "examples": [
                        {
                            "target_row": int(queries[j]),
                            "true_camera_z": float(target["truth_z"][queries[j]]),
                            "baseline_camera_z": float(
                                scores["measured_all"][offset + queries[j]]
                            ),
                            "fused_camera_z": float(z[winners[j]]),
                            "winner_source": int(cloud["source"][winners[j]]),
                            "winner_pixel": int(cloud["pixel"][winners[j]]),
                            "winner_kind": int(cloud["kind"][winners[j]]),
                            "pixel_distance": float(distance[j]),
                            "world_error_m": float(
                                np.linalg.norm(
                                    cloud["xyz"][winners[j]] - target["xyz"][queries[j]]
                                )
                            ),
                        }
                        for j in chosen
                    ],
                }
            )
        offset += len(target["truth_z"])
    write_json(
        root / "fusion-projection-loss-audit.json",
        {
            "scope": "frontmost assigned camera in each holdout only; fixed existing losses, no parameter tuning; depth-tested integer pixels then nearest image-plane query within 1.5px",
            "records": projection_loss,
        },
    )
    actor_cache = {}
    dynamic_sampled = 0
    maximum_motion = 0.0
    maximum_actor_pixel = 0.0

    def check_motion(cloud, timestamp, mode):
        nonlocal maximum_motion, dynamic_sampled, maximum_actor_pixel
        if (
            not np.isfinite(cloud["xyz"]).all()
            or (cloud["source"] < 0).any()
            or (cloud["source"] >= 1318).any()
        ):
            raise ValueError("invalid dynamic source or coordinates")
        for instance in np.unique(cloud["instance"]):
            token = tracks.tokens[int(instance)]
            rigid = (
                tracks.rigid(token)
                and tracks.tracks[token][0]["category"] != "vehicle.bus.bendy"
            )
            if mode == "single" or not rigid:
                if len(np.unique(cloud["source"][cloud["instance"] == instance])) > 1:
                    raise ValueError(
                        "single-exposure policy accumulated different source exposures"
                    )
        take = random.choice(
            len(cloud["xyz"]), min(140, len(cloud["xyz"])), replace=False
        )
        for source in np.unique(cloud["source"][take]):
            source = int(source)
            if abs(views[source]["timestamp"] - timestamp) > 100000:
                raise ValueError("dynamic source exceeds frozen neighbourhood")
            if source not in actor_cache:
                original = dict(np.load(root / "views" / f"actors-{source:04d}.npz"))
                if len(np.unique(original["pixel"])) != len(original["pixel"]):
                    raise ValueError(
                        "one source pixel assigned to multiple dynamic candidates"
                    )
                probe = random.choice(
                    len(original["xyz"]), min(80, len(original["xyz"])), replace=False
                )
                pixel = original["pixel"][probe]
                uv, _ = project(
                    original["xyz"][probe],
                    np.asarray(views[source]["intrinsic"]),
                    np.asarray(views[source]["global_from_sensor"]),
                )
                maximum_actor_pixel = max(
                    maximum_actor_pixel,
                    float(
                        np.linalg.norm(
                            uv - np.column_stack((pixel % 640, pixel // 640)), axis=1
                        ).max(initial=0)
                    ),
                )
                rgb = source_rgb(source)
                if not np.array_equal(
                    original["rgb"][probe], rgb.reshape(-1, 3)[pixel]
                ):
                    raise ValueError(
                        "dynamic source colour differs from actual RGB exposure"
                    )
                source_boxes = tracks.boxes(views[source]["timestamp"])
                labels = assign_instances(original["xyz"][probe], source_boxes)
                if (labels < 0).any():
                    raise ValueError(
                        "source actor candidate has ambiguous or invalid box membership"
                    )
                expected_instance = np.asarray(
                    [tracks.ids[b["instance_token"]] for b in source_boxes]
                )[labels]
                if not np.array_equal(expected_instance, original["instance"][probe]):
                    raise ValueError("source actor belongs to a different instance")
                actor_cache[source] = (
                    original,
                    {int(p): j for j, p in enumerate(original["pixel"])},
                )
            original, lookup = actor_cache[source]
            for row in take[cloud["source"][take] == source]:
                index = lookup[int(cloud["pixel"][row])]
                instance = int(cloud["instance"][row])
                if instance != int(original["instance"][index]) or not np.array_equal(
                    cloud["rgb"][row], original["rgb"][index]
                ):
                    raise ValueError("dynamic identity or colour mixed")
                token = tracks.tokens[instance]
                rigid = (
                    tracks.rigid(token)
                    and tracks.tracks[token][0]["category"] != "vehicle.bus.bendy"
                )
                expected = original["xyz"][index : index + 1]
                if mode != "unaligned" or not rigid:
                    first, last = (
                        tracks.box(token, views[source]["timestamp"]),
                        tracks.box(token, timestamp),
                    )
                    if first is None or last is None:
                        raise ValueError("dynamic pose extrapolated")
                    expected = warp_instance(expected, first, last)
                maximum_motion = max(
                    maximum_motion,
                    float(np.linalg.norm(cloud["xyz"][row] - expected[0])),
                )
                dynamic_sampled += 1

    for frame in display["timeline"]:
        timestamp = frame["timestamp"]
        front = views[frame["source"]]
        if front["camera"] != "CAM_FRONT" or front["timestamp"] != timestamp:
            raise ValueError("timeline does not use actual front exposure timestamps")
        for camera, row in zip(CAMERAS, frame["cameras"]):
            nearest = min(
                (v for v in views if v["camera"] == camera),
                key=lambda v: abs(v["timestamp"] - timestamp),
            )
            if (
                row["source"] != nearest["index"]
                or row["delta_us"] != nearest["timestamp"] - timestamp
                or row["available"] != (abs(row["delta_us"]) <= 125000)
            ):
                raise ValueError(
                    "preview's asynchronous camera selection or timestamp delta changed"
                )
            pose = np.asarray(nearest["global_from_sensor"])
            expected = np.eye(4)
            expected[:3, :3] = basis @ pose[:3, :3]
            expected[:3, 3] = basis @ (pose[:3, 3] - origin)
            if not np.allclose(
                row["viewer_from_camera_cv"], expected, atol=1e-10, rtol=0
            ) or not np.array_equal(row["intrinsic"], nearest["intrinsic"]):
                raise ValueError(
                    "preview camera calibration disconnected from sensor metadata"
                )
        for mode in ("single", "unaligned", "compensated"):
            with Image.open(root / frame["renders"][mode]) as image:
                if image.size != (640, 352) or image.mode != "RGB":
                    raise ValueError("invalid model RGB point render")
            cloud = dict(np.load(root / "actors" / f"{frame['frame']:03d}-{mode}.npz"))
            path = root / frame["actors"][mode]["path"]
            if (
                digest(path) != frame["actors"][mode]["sha256"]
                or path.stat().st_size != len(cloud["xyz"]) * 16
            ):
                raise ValueError("actor display buffer invalid")
            buffer = np.fromfile(
                path, dtype=[("xyz", "<f4", (3,)), ("rgb", "u1", (3,)), ("kind", "u1")]
            )
            expected = (cloud["xyz"].astype(float) - origin) @ basis.T
            if (
                not np.allclose(buffer["xyz"], expected, atol=3e-5, rtol=0)
                or not np.array_equal(buffer["rgb"], cloud["rgb"])
                or not np.array_equal(buffer["kind"], cloud["kind"])
            ):
                raise ValueError(
                    "actor preview buffer differs from timestamped world cloud"
                )
            check_motion(cloud, timestamp, mode)
        if frame["frame"] % 40 == 0:
            print(
                f"Audited timestamped actors {frame['frame'] + 1}/{len(display['timeline'])}",
                flush=True,
            )
    dynamic_groups = 0
    for record in validation["scans"]:
        scan = next(s for s in scans if s["token"] == record["scan_token"])
        if scan["token"] not in heldout or scan["timestamp"] != record["timestamp"]:
            raise ValueError("dynamic targets are not acquired at heldout scan time")
        target = dict(np.load(root / f"dynamic-targets-{record['frame']:02d}.npz"))
        raw = np.fromfile(scan["path"], dtype="<f4").reshape(-1, 5)[:, :3]
        truth = transform(raw, np.asarray(scan["global_from_sensor"]))
        if not np.allclose(
            truth[target["raw_rows"]], target["xyz"], atol=1e-10, rtol=0
        ):
            raise ValueError("dynamic target is not an actual raw return")
        boxes = tracks.boxes(scan["timestamp"])
        labels = assign_instances(truth, boxes)
        selected = np.flatnonzero(labels >= 0)
        if not np.array_equal(target["raw_rows"], selected):
            raise ValueError("fixed actor target selection changed")
        expected_instances = np.asarray(
            [tracks.ids[b["instance_token"]] for b in boxes]
        )[labels[selected]]
        if not np.array_equal(target["instance"], expected_instances):
            raise ValueError("heldout surface instance identities changed")
        for mode in ("single", "unaligned", "compensated"):
            cloud = dict(
                np.load(root / f"dynamic-heldout-{record['frame']:02d}-{mode}.npz")
            )
            check_motion(cloud, record["timestamp"], mode)
            for group in record["instances"]:
                instance = group["instance"]
                actual = cloud["xyz"][cloud["instance"] == instance]
                query = target["xyz"][target["instance"] == instance]
                distance = (
                    cKDTree(actual).query(query, k=1)[0]
                    if len(actual)
                    else np.full(len(query), np.inf)
                )
                if not np.allclose(
                    distance, target[f"distance_{mode}_{instance}"], atol=1e-10, rtol=0
                ):
                    raise ValueError("dynamic sparse surface scores changed")
                reported = group["modes"][mode]
                if reported["points"] != len(actual) or reported["targets"] != len(
                    query
                ):
                    raise ValueError("reported dynamic group counts changed")
                for radius in (0.1, 0.2, 0.5):
                    if not np.isclose(
                        reported[f"support_{radius:g}m"],
                        np.mean(distance <= radius),
                        atol=1e-12,
                        rtol=0,
                    ):
                        raise ValueError(
                            "reported dynamic support differs from actual score"
                        )
                if not np.isclose(
                    reported["clipped_mean_1m"],
                    np.minimum(distance, 1).mean(),
                    atol=1e-12,
                    rtol=0,
                ):
                    raise ValueError(
                        "reported dynamic distance differs from actual score"
                    )
                dynamic_groups += 1
    if maximum_motion > 0.0002 or maximum_actor_pixel > 0.05:
        raise ValueError(
            "saved actor motion or source pixel projection differs from its provenance"
        )
    fence = verify_runtime_sources(root.parent)
    report = {
        "status": "passed",
        "input_images": 1318,
        "unique_owners": len(owner),
        "windows": len(windows),
        "globally_excluded_scans": len(heldout),
        "static_provenance_points": sampled,
        "maximum_source_xyz_error_m": maximum_xyz,
        "fresh_static_score_samples": checked_targets,
        "dynamic_provenance_points": dynamic_sampled,
        "maximum_motion_xyz_error_m": maximum_motion,
        "maximum_actor_source_pixel_error_px": maximum_actor_pixel,
        "dynamic_group_mode_scores": dynamic_groups,
        "key_measured_geometry_exactly_matches_previous": control_equal,
        "measured_samples_preserved_verbatim_in_fusion": True,
        "old_runtime_source_fence": fence,
        "scope": "sampled geometry/colour/motion provenance plus all group-mode sparse dynamic scores; not dense geometric truth or hardware renderer qualification",
        "seconds": time.perf_counter() - started,
    }
    snapshot = root / "audit-source"
    snapshot.mkdir(exist_ok=True)
    shutil.copyfile(__file__, snapshot / Path(__file__).name)
    report["audit_source_sha256"] = digest(__file__)
    write_json(root / "sweeps-artifact-audit.json", report)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    audit(parser.parse_args().output)
