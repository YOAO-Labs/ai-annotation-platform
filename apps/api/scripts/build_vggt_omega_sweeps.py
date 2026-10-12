"""Calibrate all exposures, fuse static geometry and compare timestamped actors."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.spatial import cKDTree

from build_vggt_omega_sequence import (
    VIEWER_FROM_WORLD,
    camera_poses,
    combine,
    empty,
    from_depth,
    packed,
    render_points,
    similarity_align,
    subset,
    viewer_pose,
)
from omega_densification import (
    cloud_depth_at_targets,
    error_summary,
    lidar_evidence,
    local_correction,
    project,
    robust_scale,
    sample_image,
    transform,
    visible_rows,
)
from omega_dynamic import (
    ActorTracks,
    assign_instances,
    fast_voxel,
    nearest_indices,
    warp_instance,
)
from qualify_vggt_omega import CAMERAS, SceneData, digest
from vggt_common import (
    actor_ray_mask,
    cross_view_support,
    points_in_actors,
    verify_runtime_sources,
    write_json,
)

METHODS = {
    "fused_all": "全部扫描 · 实测 + 校准模型",
    "calibrated_all": "全部扫描 · 校准模型",
    "measured_all": "全部扫描 · 实测 LiDAR",
    "fused_key": "关键扫描 · 实测 + 校准模型",
    "calibrated_key": "关键扫描 · 校准模型",
    "measured_key": "关键扫描 · 实测 LiDAR",
    "raw": "窗口原生几何 · 逐窗 Sim3",
}
MODES = ("single", "unaligned", "compensated")


def parallel(function, values, workers=8):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(function, values))


def merge_actors(items):
    if not items:
        return {**empty(), "instance": np.empty(0, np.int16)}
    return {key: np.concatenate([item[key] for item in items]) for key in items[0]}


def export(root, method, item, origin):
    before = len(item["xyz"])
    item = subset(item, fast_voxel(item["xyz"]))
    destination = root / f"{method}-full.npz"
    np.savez_compressed(destination, **item)
    lod = {}
    for budget in (120000, 350000, 1200000):
        indices = np.linspace(
            0, len(item["xyz"]) - 1, min(budget, len(item["xyz"])), dtype=np.int64
        )
        lod[str(budget)] = packed(
            root / f"{method}-{budget}.bin", subset(item, indices), origin
        )
        np.save(root / f"{method}-{budget}-indices.npy", indices)
    return item, {
        "points": len(item["xyz"]),
        "before_voxel": before,
        "full_file": destination.name,
        "full_sha256": digest(destination),
        "lod": lod,
        "source_counts": {
            str(int(i)): int(n)
            for i, n in zip(*np.unique(item["source"], return_counts=True))
        },
    }


def view_references(views, index, by_camera):
    view = views[index]
    own = by_camera[view["camera"]]
    offset = own.index(index)
    refs = [own[j] for j in (offset - 1, offset + 1) if 0 <= j < len(own)]
    channel = CAMERAS.index(view["camera"])
    for camera in (CAMERAS[(channel - 1) % 6], CAMERAS[(channel + 1) % 6]):
        candidates = [
            i
            for i in by_camera[camera]
            if abs(views[i]["timestamp"] - view["timestamp"]) <= 125000
        ]
        if candidates:
            refs.append(
                min(
                    candidates,
                    key=lambda i: (
                        abs(views[i]["timestamp"] - view["timestamp"]),
                        views[i]["timestamp"],
                    ),
                )
            )
    return sorted(set(refs) - {index})


def allowed_scans(protocol, view, branch):
    eligible = [
        s
        for s in protocol["scans"]
        if not s["heldout"] and (branch == "all" or s["is_key_frame"])
    ]
    take = nearest_indices(eligible, view["timestamp"], 1000000, 4)
    return [eligible[i]["index"] for i in take]


class Reconstruction:
    def __init__(self, root):
        self.root = root
        self.protocol = json.loads((root / "sweeps-protocol.json").read_text())
        self.inference = json.loads((root / "sweeps-inference.json").read_text())
        if self.inference["status"] != "completed" or self.inference[
            "protocol_sha256"
        ] != digest(root / "sweeps-protocol.json"):
            raise ValueError("inference is incomplete or protocol changed")
        self.data = SceneData(Path(self.protocol["data_root"]))
        self.scene = self.protocol["scene"]
        self.tracks = ActorTracks(self.data.tracks[self.scene])
        self.views, self.scans = self.protocol["views"], self.protocol["scans"]
        self.poses = np.asarray([v["global_from_sensor"] for v in self.views])
        self.ks = np.asarray([v["intrinsic"] for v in self.views])
        self.by_camera = {
            c: [v["index"] for v in self.views if v["camera"] == c] for c in CAMERAS
        }
        self.origin = self.poses[0, :3, 3].copy()
        self.origin[2] = np.median(self.poses[:, 2, 3]) - 1.5
        self.h, self.w = 352, 640
        self.depth = np.empty((len(self.views), self.h, self.w), np.float32)
        self.confidence = np.empty_like(self.depth)
        self.raw_scale = np.empty(len(self.views))
        self.raw_k = np.empty_like(self.ks)
        self.raw_pose = np.empty_like(self.poses)
        self.rgb = np.empty((*self.depth.shape, 3), np.uint8)
        self.masks = np.empty(self.depth.shape, bool)
        self.static_scans, self.actor_scans = {}, {}
        self.records = [
            {
                "index": i,
                "owner_window": v["owner_window"],
                "camera": v["camera"],
                "timestamp": v["timestamp"],
                "calibration": {},
            }
            for i, v in enumerate(self.views)
        ]
        self.window_stats = []
        self.assets = {}
        self.clouds = {}
        self.actors = []
        self.actor_stats = []

    def load(self):
        for name, expected in self.protocol["metadata_sha256"].items():
            if (
                digest(Path(self.protocol["data_root"]) / "v1.0-mini" / name)
                != expected
            ):
                raise ValueError("metadata changed")
        for row in self.views + self.scans:
            if digest(row["path"]) != row["sha256"]:
                raise ValueError("sensor input changed")
            if not np.allclose(
                self.data.pose(self.data.tables["sample_data"][row["token"]]),
                row["global_from_sensor"],
                atol=1e-10,
                rtol=0,
            ):
                raise ValueError("sensor pose changed")
        for record in self.inference["windows"]:
            path = self.root / record["path"]
            if digest(path) != record["sha256"]:
                raise ValueError("window predictions changed")
            arrays = np.load(path)
            indices = np.asarray(record["indices"])
            owner_local = np.array(
                [self.views[i]["owner_window"] == record["index"] for i in indices]
            )
            chosen = indices[owner_local]
            self.depth[chosen] = arrays["depth"][owner_local]
            self.confidence[chosen] = arrays["confidence"][owner_local]
            predicted = camera_poses(arrays["extrinsic"])
            scale, rotation, translation, residual, keep = similarity_align(
                predicted[:, :3, 3], self.poses[indices, :3, 3]
            )
            aligned = predicted.copy()
            aligned[:, :3, :3] = rotation @ predicted[:, :3, :3]
            aligned[:, :3, 3] = predicted[:, :3, 3] @ rotation.T * scale + translation
            self.raw_pose[chosen] = aligned[owner_local]
            self.raw_scale[chosen] = scale
            k = arrays["intrinsic"].copy()
            k[:, :2, 2] -= 0.5
            self.raw_k[chosen] = k[owner_local]
            self.window_stats.append(
                {
                    "window": record["index"],
                    "owned_views": chosen.tolist(),
                    "native_sim3_scale": scale,
                    "rotation": rotation,
                    "translation": translation,
                    "fit_cameras": int(keep.sum()),
                    "camera_rmse_m": float(np.sqrt(np.mean(residual**2))),
                    "calibration": {},
                }
            )
            print(
                f"Loaded owner predictions {record['index'] + 1}/{len(self.inference['windows'])}",
                flush=True,
            )
        (self.root / "rgb").mkdir()
        (self.root / "views").mkdir()
        (self.root / "actors").mkdir()
        (self.root / "renders").mkdir()

        def image(i):
            view = self.views[i]
            with Image.open(view["path"]) as source:
                self.rgb[i] = np.asarray(
                    source.convert("RGB").resize(
                        (self.w, self.h), Image.Resampling.BICUBIC
                    )
                )
            Image.fromarray(self.rgb[i]).save(
                self.root / "rgb" / f"{i:04d}.jpg", quality=94
            )
            self.masks[i] = actor_ray_mask(
                self.ks[i],
                (self.w, self.h),
                self.poses[i],
                self.data.boxes(self.scene, view["timestamp"]),
                0.3,
            )
            if i % 100 == 0:
                print(
                    f"Prepared real-time actor masks {i + 1}/{len(self.views)}",
                    flush=True,
                )

        parallel(image, range(len(self.views)))
        for scan in self.scans:
            if scan["heldout"]:
                continue
            raw = np.fromfile(scan["path"], dtype="<f4").reshape(-1, 5)[:, :3]
            xyz = transform(raw, np.asarray(scan["global_from_sensor"]))
            boxes = self.data.boxes(self.scene, scan["timestamp"])
            keep = ~points_in_actors(xyz, boxes, 0.3)
            keep &= (
                np.linalg.norm(
                    xyz - np.asarray(scan["global_from_sensor"])[:3, 3], axis=1
                )
                <= 80
            )
            rows = np.flatnonzero(keep)
            self.static_scans[scan["index"]] = {
                "xyz": xyz[rows].astype(np.float32),
                "rgb": np.full((len(rows), 3), 150, np.uint8),
                "source": np.full(len(rows), -scan["index"] - 1, np.int16),
                "pixel": rows.astype(np.int32),
                "kind": np.zeros(len(rows), np.uint8),
            }
            strict = self.tracks.boxes(scan["timestamp"])
            labels = assign_instances(xyz, strict)
            selected = np.flatnonzero(labels >= 0)
            self.actor_scans[scan["index"]] = {
                "xyz": xyz[selected],
                "raw_rows": selected,
                "instances": np.asarray(
                    [self.tracks.ids[b["instance_token"]] for b in strict], np.int16
                )[labels[selected]],
            }
        print(f"Loaded {len(self.static_scans)} globally permitted scans", flush=True)

    def prepare_anchors(self, branch):
        def prepare(i):
            selected = allowed_scans(self.protocol, self.views[i], branch)
            cloud = combine([self.static_scans[s] for s in selected])
            uv, z = project(cloud["xyz"], self.ks[i], self.poses[i])
            take = visible_rows(uv, z, (self.h, self.w))
            pixel = np.rint(uv[take]).astype(int)
            take = take[~self.masks[i, pixel[:, 1], pixel[:, 0]]]
            identity = (
                -(cloud["source"][take].astype(np.int64) + 1)
            ) * 1000000 + cloud["pixel"][take]
            self.records[i]["calibration"][branch] = {
                "anchor_indices": selected,
                "anchor_tokens": [self.scans[s]["token"] for s in selected],
                "observations": len(take),
            }
            return {
                "uv": uv[take],
                "z": z[take],
                "ids": identity,
                "predicted": sample_image(self.depth[i], uv[take]),
            }

        anchors = parallel(prepare, range(len(self.views)))
        scales = np.empty(len(self.views))
        for window in self.window_stats:
            items = [anchors[i] for i in window["owned_views"]]
            scale, stats = robust_scale(
                np.concatenate([a["predicted"] for a in items]),
                np.concatenate([a["z"] for a in items]),
                np.concatenate([a["ids"] for a in items]),
            )
            window["calibration"][branch] = stats
            scales[window["owned_views"]] = scale
        return anchors, scales

    def static(self, branch):
        print(f"Preparing {branch} calibration", flush=True)
        anchors, scales = self.prepare_anchors(branch)
        corrected = np.empty_like(self.depth)
        locals_mask = np.empty(self.depth.shape, bool)

        def correct(i):
            corrected[i], locals_mask[i], stats = local_correction(
                self.depth[i] * scales[i],
                self.rgb[i],
                anchors[i]["uv"],
                anchors[i]["z"],
            )
            self.records[i]["calibration"][branch]["local"] = stats
            if i % 100 == 0:
                print(
                    f"{branch} local calibration {i + 1}/{len(self.views)}", flush=True
                )

        parallel(correct, range(len(self.views)))
        masked = np.where(self.masks, np.nan, corrected)
        refs = [
            view_references(self.views, i, self.by_camera)
            for i in range(len(self.views))
        ]

        def generate(i):
            conf = self.confidence[i] >= np.quantile(self.confidence[i], 0.2)
            conf[int(self.h * 0.95) :] = False
            support, conflict = cross_view_support(
                corrected[i],
                self.ks[i],
                self.poses[i],
                [
                    {
                        "model": masked[j],
                        "intrinsic": self.ks[j],
                        "reference_from_camera": self.poses[j],
                    }
                    for j in refs[i]
                ],
                absolute_m=0.3,
                relative=0.03,
                pixel_tolerance=2,
            )
            direct, veto = lidar_evidence(
                corrected[i], anchors[i]["uv"], anchors[i]["z"]
            )
            valid = conf & ~self.masks[i] & (corrected[i] >= 1) & (corrected[i] <= 80)
            accepted = valid & ((support >= 1) | direct) & (conflict == 0) & ~veto
            item = from_depth(
                corrected[i], self.rgb[i], accepted, self.ks[i], self.poses[i], i, 1
            )
            item = subset(item, fast_voxel(item["xyz"]))
            np.savez_compressed(
                self.root / "views" / f"{branch}-{i:04d}.npz",
                corrected=corrected[i],
                accepted=accepted,
                support=support,
                conflict=conflict,
                lidar_conflict=veto,
                local_supported=locals_mask[i],
            )
            self.records[i]["calibration"][branch].update(
                scale=float(scales[i]),
                source_pixels=int(accepted.sum()),
                view_voxel_points=len(item["xyz"]),
                references=refs[i],
            )
            if i % 100 == 0:
                print(
                    f"{branch} consistency and samples {i + 1}/{len(self.views)}",
                    flush=True,
                )
            return item

        items = parallel(generate, range(len(self.views)))
        name = f"calibrated_{branch}"
        self.clouds[name], self.assets[name] = export(
            self.root, name, combine(items), self.origin
        )
        selected = [
            item
            for index, item in self.static_scans.items()
            if branch == "all" or self.scans[index]["is_key_frame"]
        ]
        name = f"measured_{branch}"
        self.clouds[name], self.assets[name] = export(
            self.root, name, combine(selected), self.origin
        )
        name = f"fused_{branch}"
        self.clouds[name], self.assets[name] = export(
            self.root,
            name,
            combine(
                [self.clouds[f"measured_{branch}"], self.clouds[f"calibrated_{branch}"]]
            ),
            self.origin,
        )
        if branch == "all":
            self.all_corrected = corrected
            self.all_scales = scales
        print(
            f"Completed {branch}: {self.assets[f'calibrated_{branch}']['points']:,} model / {self.assets[name]['points']:,} fused points",
            flush=True,
        )

    def raw(self):
        def generate(i):
            z = self.depth[i] * self.raw_scale[i]
            mask = (
                (self.confidence[i] >= np.quantile(self.confidence[i], 0.2))
                & ~self.masks[i]
                & (z >= 1)
                & (z <= 80)
            )
            mask[int(self.h * 0.95) :] = False
            item = from_depth(
                z, self.rgb[i], mask, self.raw_k[i], self.raw_pose[i], i, 1
            )
            return subset(item, fast_voxel(item["xyz"]))

        items = parallel(generate, range(len(self.views)))
        self.clouds["raw"], self.assets["raw"] = export(
            self.root, "raw", combine(items), self.origin
        )

    def overlap(self):
        records = []
        for window in self.window_stats:
            index = window["window"]
            arrays = np.load(self.root / self.inference["windows"][index]["path"])
            other_depth, other_confidence = arrays["depth"], arrays["confidence"]
            for branch in ("key", "all"):
                scale = window["calibration"][branch]["scale"]
                differences = []
                repeated = 0
                for local, source in enumerate(self.protocol["windows"][index]):
                    owner = self.views[source]["owner_window"]
                    if owner == index:
                        continue
                    owner_scale = self.window_stats[owner]["calibration"][branch][
                        "scale"
                    ]
                    a = other_depth[local].ravel()[::64] * scale
                    b = self.depth[source].ravel()[::64] * owner_scale
                    valid = (
                        ~self.masks[source].ravel()[::64]
                        & (a >= 1)
                        & (a <= 80)
                        & (b >= 1)
                        & (b <= 80)
                    )
                    valid &= other_confidence[local].ravel()[::64] >= np.quantile(
                        other_confidence[local], 0.2
                    )
                    valid &= self.confidence[source].ravel()[::64] >= np.quantile(
                        self.confidence[source], 0.2
                    )
                    differences.append(np.abs(a[valid] - b[valid]))
                    repeated += 1
                values = np.concatenate(differences) if differences else np.empty(0)
                records.append(
                    {
                        "window": index,
                        "branch": branch,
                        "repeated_exposures": repeated,
                        "sampled_static_pixels": len(values),
                        "median_camera_z_difference_m": float(np.median(values))
                        if len(values)
                        else None,
                        "p90_camera_z_difference_m": float(np.quantile(values, 0.9))
                        if len(values)
                        else None,
                    }
                )
        write_json(
            self.root / "overlap-validation.json",
            {
                "scope": "same RGB under different window contexts after per-window metric scale; not independent temporal support and not ground-truth surface error",
                "records": records,
            },
        )

    def colour_scans(self):
        def colour(index):
            item = self.static_scans[index]
            scan = self.scans[index]
            best = np.full(len(item["xyz"]), -np.inf)
            for camera in CAMERAS:
                candidates = self.by_camera[camera]
                i = min(
                    candidates,
                    key=lambda i: abs(self.views[i]["timestamp"] - scan["timestamp"]),
                )
                if abs(self.views[i]["timestamp"] - scan["timestamp"]) > 125000:
                    continue
                uv, z = project(item["xyz"], self.ks[i], self.poses[i])
                take = visible_rows(uv, z, (self.h, self.w))
                margin = np.minimum.reduce(
                    (
                        uv[take, 0],
                        self.w - 1 - uv[take, 0],
                        uv[take, 1],
                        self.h - 1 - uv[take, 1],
                    )
                )
                better = margin > best[take]
                take, margin = take[better], margin[better]
                colours = sample_image(self.rgb[i], uv[take])
                good = np.isfinite(colours).all(axis=1)
                take, margin, colours = take[good], margin[good], colours[good]
                item["rgb"][take] = np.rint(colours).clip(0, 255).astype(np.uint8)
                best[take] = margin

        parallel(colour, list(self.static_scans))

    def actor_candidates(self):
        def generate(i):
            view = self.views[i]
            boxes = self.tracks.boxes(view["timestamp"])
            token_to_box = {b["instance_token"]: b for b in boxes}
            observations = {}
            indices = nearest_indices(self.scans, view["timestamp"], 150000)
            for s in indices:
                if self.scans[s]["heldout"]:
                    continue
                scan = self.actor_scans[s]
                for instance in np.unique(scan["instances"]):
                    token = self.tracks.tokens[int(instance)]
                    target = token_to_box.get(token)
                    source = self.tracks.box(token, self.scans[s]["timestamp"])
                    if target is None or source is None:
                        continue
                    selected = scan["instances"] == instance
                    moved = warp_instance(scan["xyz"][selected], source, target)
                    uv, z = project(moved, self.ks[i], self.poses[i])
                    ids = s * 1000000 + scan["raw_rows"][selected]
                    observations.setdefault(token, []).append((uv, z, ids))
            conf = self.confidence[i] >= np.quantile(self.confidence[i], 0.2)
            conf[int(self.h * 0.95) :] = False
            base = self.depth[i] * self.all_scales[i]
            items, stats = [], []
            for own, box in enumerate(boxes):
                token = box["instance_token"]
                if token not in observations:
                    continue
                uv, z, ids = [
                    np.concatenate([a[k] for a in observations[token]])
                    for k in range(3)
                ]
                take = visible_rows(uv, z, (self.h, self.w))
                uv, z, ids = uv[take], z[take], ids[take]
                predicted = sample_image(base, uv)
                good = np.isfinite(predicted) & (predicted >= 1) & (predicted <= 80)
                uv, z, ids, predicted = uv[good], z[good], ids[good], predicted[good]
                if len(np.unique(ids)) < 8:
                    continue
                ratio = float(np.exp(np.median(np.log(z / predicted))))
                ratio = float(np.clip(ratio, 0.5, 2.0))
                mask = (
                    actor_ray_mask(
                        self.ks[i], (self.w, self.h), self.poses[i], [box], 0.3
                    )
                    & conf
                )
                corrected, local, _ = local_correction(
                    np.where(mask, base * ratio, np.nan), self.rgb[i], uv, z
                )
                direct, veto = lidar_evidence(
                    corrected, uv, z, absolute=0.4, relative=0.04
                )
                valid = (
                    mask
                    & (corrected >= 1)
                    & (corrected <= 80)
                    & ~veto
                    & (local | direct)
                )
                item = from_depth(
                    corrected, self.rgb[i], valid, self.ks[i], self.poses[i], i, 2
                )
                labels = assign_instances(item["xyz"], boxes)
                item = subset(item, labels == own)
                item["instance"] = np.full(
                    len(item["xyz"]), self.tracks.ids[token], np.int16
                )
                item = subset(item, fast_voxel(item["xyz"], 0.05))
                items.append(item)
                stats.append(
                    {
                        "instance": self.tracks.ids[token],
                        "anchor_tokens": [
                            self.scans[s]["token"]
                            for s in indices
                            if not self.scans[s]["heldout"]
                        ],
                        "unique_anchor_returns": len(np.unique(ids)),
                        "object_depth_ratio": ratio,
                        "points": len(item["xyz"]),
                    }
                )
            item = merge_actors(items)
            if len(item["xyz"]):
                _, z = project(item["xyz"], self.ks[i], self.poses[i])
                order = np.argsort(z, kind="stable")
                _, first = np.unique(item["pixel"][order], return_index=True)
                take = np.sort(order[first])
                self.records[i]["dynamic_pixel_collisions_removed"] = len(
                    item["xyz"]
                ) - len(take)
                item = subset(item, take)
            else:
                self.records[i]["dynamic_pixel_collisions_removed"] = 0
            np.savez_compressed(self.root / "views" / f"actors-{i:04d}.npz", **item)
            if i % 100 == 0:
                print(
                    f"Instance-isolated dynamic calibration {i + 1}/{len(self.views)}",
                    flush=True,
                )
            return item, stats

        results = parallel(generate, range(len(self.views)))
        self.actors = [r[0] for r in results]
        self.actor_stats = [r[1] for r in results]
        self.dynamic_reference = self.all_corrected.copy()
        for i, item in enumerate(self.actors):
            if len(item["xyz"]):
                _, z = project(item["xyz"], self.ks[i], self.poses[i])
                nearest = np.full(self.h * self.w, np.inf)
                np.minimum.at(nearest, item["pixel"], z)
                valid = np.isfinite(nearest)
                self.dynamic_reference[i].flat[valid] = nearest[valid]

    def is_rigid(self, token):
        category = self.tracks.tracks[token][0]["category"]
        return self.tracks.rigid(token) and category != "vehicle.bus.bendy"

    def dynamic_at(self, timestamp):
        selected = nearest_indices(self.views, timestamp, 100000)
        items = [self.actors[i] for i in selected]
        candidates = merge_actors(items)
        results = {mode: [] for mode in MODES}
        pre_ids, instance_recipes = [], []
        reference_views = []
        for camera in CAMERAS:
            reference = min(
                self.by_camera[camera],
                key=lambda i: abs(self.views[i]["timestamp"] - timestamp),
            )
            if abs(self.views[reference]["timestamp"] - timestamp) <= 125000:
                reference_views.append(reference)
        for instance in np.unique(candidates["instance"]):
            token = self.tracks.tokens[int(instance)]
            target = self.tracks.box(token, timestamp)
            if target is None:
                continue
            item = subset(candidates, candidates["instance"] == instance)
            sources = sorted(
                np.unique(item["source"]),
                key=lambda i: (
                    abs(self.views[int(i)]["timestamp"] - timestamp),
                    int(i),
                ),
            )
            usable = [
                int(i)
                for i in sources
                if self.tracks.box(token, self.views[int(i)]["timestamp"]) is not None
            ]
            if not usable:
                continue
            item = subset(item, np.isin(item["source"], usable))
            single = subset(item, item["source"] == usable[0])
            if not self.is_rigid(token):
                item = single
            ids = item["source"].astype(np.int64) * self.h * self.w + item["pixel"]
            pre_ids.append(ids)
            moved = {key: value.copy() for key, value in item.items()}
            for source in np.unique(item["source"]):
                take = item["source"] == source
                source_box = self.tracks.box(
                    token, self.views[int(source)]["timestamp"]
                )
                moved["xyz"][take] = warp_instance(
                    item["xyz"][take], source_box, target
                )
            one = subset(moved, moved["source"] == usable[0])
            spread = {}
            for name, geometry in (("unaligned", item), ("compensated", moved)):
                if not self.is_rigid(token):
                    geometry = one
                if len(geometry["xyz"]):
                    local_xyz = (
                        geometry["xyz"] - np.asarray(target["translation"])
                    ) @ np.asarray(target["rotation_matrix"])
                    spread[name] = (
                        np.quantile(local_xyz, 0.95, axis=0)
                        - np.quantile(local_xyz, 0.05, axis=0)
                    ).tolist()
            instance_recipes.append(
                {
                    "instance": int(instance),
                    "token": token,
                    "rigid": self.is_rigid(token),
                    "single_source": usable[0],
                    "pre_source_ids": len(ids),
                    "pre_source_ids_sha256": hashlib.sha256(
                        np.sort(ids).tobytes()
                    ).hexdigest(),
                    "pre_voxel_local_90percent_extent_m": spread,
                }
            )
            for mode, cloud in (
                ("single", one),
                ("unaligned", item),
                ("compensated", moved),
            ):
                # Nonrigid actors use the same sole exposure in all three modes.
                if not self.is_rigid(token):
                    cloud = one
                visible = np.zeros(len(cloud["xyz"]), bool)
                for reference in reference_views:
                    reference_box = self.tracks.box(
                        token, self.views[reference]["timestamp"]
                    )
                    if reference_box is None:
                        continue
                    at_exposure = warp_instance(cloud["xyz"], target, reference_box)
                    uv, z = project(
                        at_exposure, self.ks[reference], self.poses[reference]
                    )
                    observed = sample_image(self.dynamic_reference[reference], uv)
                    visible |= (
                        (z >= 1)
                        & (z <= 80)
                        & np.isfinite(observed)
                        & (z <= observed + np.maximum(0.5, 0.05 * observed))
                    )
                cloud = subset(cloud, visible)
                cloud = subset(cloud, fast_voxel(cloud["xyz"], 0.05))
                results[mode].append(cloud)
        output = {mode: merge_actors(values) for mode, values in results.items()}
        ids = np.sort(np.concatenate(pre_ids)) if pre_ids else np.empty(0, np.int64)
        return output, {
            "timestamp": timestamp,
            "pre_source_ids": len(ids),
            "pre_source_ids_sha256": hashlib.sha256(ids.tobytes()).hexdigest(),
            "source_views": selected,
            "occlusion_reference_views": reference_views,
            "instances": instance_recipes,
            "points": {mode: len(item["xyz"]) for mode, item in output.items()},
        }

    def score_static(self):
        targets = []
        values = {method: [] for method in METHODS}
        for frozen in self.protocol["fixed_static_targets"]:
            if digest(frozen["path"]) != frozen["sha256"]:
                raise ValueError("frozen static targets changed")
            target = np.load(frozen["path"])
            truth = target["truth_z"]
            targets.append(truth)
            for method, cloud in self.clouds.items():
                prediction = np.full(len(truth), np.nan)
                for old, new in frozen["view_mapping"].items():
                    take = target["view"] == int(old)
                    i = int(new)
                    prediction[take] = cloud_depth_at_targets(
                        cloud["xyz"],
                        target["uv"][take],
                        self.ks[i],
                        self.poses[i],
                        (self.h, self.w),
                        radius=1.5,
                    )
                values[method].append(prediction)
            print(f"Scored frozen static holdout {frozen['frame']}", flush=True)
        truth = np.concatenate(targets)
        scored = {key: np.concatenate(value) for key, value in values.items()}
        np.savez_compressed(
            self.root / "static-scored-depths.npz", truth=truth, **scored
        )
        validation = {
            "targets": len(truth),
            "metrics": {
                key: error_summary(value, truth) for key, value in scored.items()
            },
            "comparisons": {},
        }
        for branch in ("key", "all"):
            baseline, fused = scored[f"measured_{branch}"], scored[f"fused_{branch}"]
            base_good = np.isfinite(baseline) & (np.abs(baseline - truth) <= 0.5)
            fused_good = np.isfinite(fused) & (np.abs(fused - truth) <= 0.5)
            common = np.isfinite(baseline) & np.isfinite(fused)
            validation["comparisons"][branch] = {
                "new_correct": int((fused_good & ~base_good).sum()),
                "lost_correct": int((base_good & ~fused_good).sum()),
                "common_targets": int(common.sum()),
                "common_measured": error_summary(baseline, truth, common),
                "common_fused": error_summary(fused, truth, common),
            }
        write_json(self.root / "static-validation.json", validation)
        return validation

    def score_dynamic(self):
        records, arrays, groups = [], {}, []
        for scan in self.scans:
            if scan["key_frame"] not in self.protocol["heldout_key_frames"]:
                continue
            if not scan["heldout"]:
                raise ValueError("dynamic evaluation scan must be globally excluded")
            raw = np.fromfile(scan["path"], dtype="<f4").reshape(-1, 5)[:, :3]
            xyz = transform(raw, np.asarray(scan["global_from_sensor"]))
            boxes = self.tracks.boxes(scan["timestamp"])
            labels = assign_instances(xyz, boxes)
            clouds, recipe = self.dynamic_at(scan["timestamp"])
            result = {
                "frame": scan["key_frame"],
                "scan_token": scan["token"],
                "timestamp": scan["timestamp"],
                "recipe": recipe,
                "instances": [],
            }
            scan_arrays = {
                "xyz": xyz[labels >= 0],
                "raw_rows": np.flatnonzero(labels >= 0),
                "instance": np.asarray(
                    [self.tracks.ids[b["instance_token"]] for b in boxes], np.int16
                )[labels[labels >= 0]],
            }
            for mode, cloud in clouds.items():
                np.savez_compressed(
                    self.root / f"dynamic-heldout-{scan['key_frame']:02d}-{mode}.npz",
                    **cloud,
                )
            for j, box in enumerate(boxes):
                take = labels == j
                truth = xyz[take]
                if not len(truth):
                    continue
                instance = self.tracks.ids[box["instance_token"]]
                record = {
                    "instance": instance,
                    "token": box["instance_token"],
                    "category": box["category"],
                    "rigid": self.is_rigid(box["instance_token"]),
                    "targets": len(truth),
                    "modes": {},
                }
                before = self.tracks.box(
                    box["instance_token"], scan["timestamp"] - 50000
                )
                after = self.tracks.box(
                    box["instance_token"], scan["timestamp"] + 50000
                )
                speed = (
                    float(
                        np.linalg.norm(
                            (
                                np.asarray(after["translation"])
                                - np.asarray(before["translation"])
                            )[:2]
                        )
                        / 0.1
                    )
                    if before is not None and after is not None
                    else None
                )
                record["speed_mps"] = speed
                record["moving_rigid"] = (
                    record["rigid"] and speed is not None and speed >= 0.5
                )
                distance_group = {
                    "token": box["instance_token"],
                    "moving_rigid": record["moving_rigid"],
                    "distances": {},
                }
                for mode, cloud in clouds.items():
                    points = cloud["xyz"][cloud["instance"] == instance]
                    distance = (
                        cKDTree(points).query(truth, k=1)[0]
                        if len(points)
                        else np.full(len(truth), np.inf)
                    )
                    arrays.setdefault(mode, []).append(distance)
                    distance_group["distances"][mode] = distance
                    scan_arrays[f"distance_{mode}_{instance}"] = distance
                    record["modes"][mode] = {
                        "points": len(points),
                        "targets": len(truth),
                        "support_0.1m": float(np.mean(distance <= 0.1)),
                        "support_0.2m": float(np.mean(distance <= 0.2)),
                        "support_0.5m": float(np.mean(distance <= 0.5)),
                        "clipped_mean_1m": float(np.minimum(distance, 1).mean()),
                    }
                result["instances"].append(record)
                groups.append(distance_group)
            np.savez_compressed(
                self.root / f"dynamic-targets-{scan['key_frame']:02d}.npz",
                **scan_arrays,
            )
            records.append(result)
        summary = {}
        for mode, parts in arrays.items():
            distance = np.concatenate(parts)
            grouped = [r["modes"][mode] for scan in records for r in scan["instances"]]
            summary[mode] = {
                "targets": len(distance),
                "instance_scan_groups": len(grouped),
                "support_0.1m": float(np.mean(distance <= 0.1)),
                "support_0.2m": float(np.mean(distance <= 0.2)),
                "support_0.5m": float(np.mean(distance <= 0.5)),
                "clipped_mean_1m": float(np.minimum(distance, 1).mean()),
                "macro_instance_scan_support_0.2m": float(
                    np.mean([g["support_0.2m"] for g in grouped])
                ),
            }

        def group_summary(selected):
            summary = {}
            for mode in MODES:
                parts = [g["distances"][mode] for g in selected]
                distance = np.concatenate(parts) if parts else np.empty(0)
                summary[mode] = {
                    "targets": len(distance),
                    "instance_scan_groups": len(parts),
                    "support_0.2m": float(np.mean(distance <= 0.2))
                    if len(distance)
                    else None,
                    "support_0.5m": float(np.mean(distance <= 0.5))
                    if len(distance)
                    else None,
                    "clipped_mean_1m": float(np.minimum(distance, 1).mean())
                    if len(distance)
                    else None,
                    "macro_instance_scan_support_0.2m": float(
                        np.mean([np.mean(p <= 0.2) for p in parts])
                    )
                    if parts
                    else None,
                }
            return summary

        moving = [g for g in groups if g["moving_rigid"]]
        gains = {}
        for group_name, selected in (("all", groups), ("moving_rigid", moving)):
            unaligned = (
                np.concatenate([g["distances"]["unaligned"] for g in selected])
                if selected
                else np.empty(0)
            )
            compensated = (
                np.concatenate([g["distances"]["compensated"] for g in selected])
                if selected
                else np.empty(0)
            )
            gains[group_name] = {
                "targets": len(unaligned),
                "new_supported_0.2m": int(
                    ((compensated <= 0.2) & (unaligned > 0.2)).sum()
                ),
                "lost_supported_0.2m": int(
                    ((unaligned <= 0.2) & (compensated > 0.2)).sum()
                ),
            }
        validation = {
            "summary": summary,
            "moving_rigid": group_summary(moving),
            "diagnostics": {
                token: group_summary([g for g in groups if g["token"] == token])
                for token in self.protocol["dynamic"]["diagnostic_instances"]
            },
            "compensation_vs_unaligned": gains,
            "scans": records,
            "scope": "sparse heldout surface-return support proxy, not dense accuracy or photometric sharpness; oracle instance motion",
        }
        write_json(self.root / "dynamic-validation.json", validation)
        return validation

    def timeline(self):
        records = []
        for frame, source in enumerate(self.by_camera["CAM_FRONT"]):
            timestamp = self.views[source]["timestamp"]
            clouds, recipe = self.dynamic_at(timestamp)
            actor_assets = {}
            for mode, item in clouds.items():
                destination = self.root / "actors" / f"{frame:03d}-{mode}"
                asset = packed(destination.with_suffix(".bin"), item, self.origin)
                asset["path"] = f"actors/{asset['path']}"
                np.savez_compressed(destination.with_suffix(".npz"), **item)
                actor_assets[mode] = asset
            camera_rows = []
            for camera in CAMERAS:
                i = min(
                    self.by_camera[camera],
                    key=lambda i: abs(self.views[i]["timestamp"] - timestamp),
                )
                view = self.views[i]
                dt = view["timestamp"] - timestamp
                camera_rows.append(
                    {
                        "source": i,
                        "frame": frame,
                        "camera": camera,
                        "timestamp": view["timestamp"],
                        "delta_us": dt,
                        "available": abs(dt) <= 125000,
                        "seconds": (timestamp - self.views[0]["timestamp"]) / 1e6,
                        "rgb": f"rgb/{i:04d}.jpg",
                        "intrinsic": self.ks[i],
                        "viewer_from_camera_cv": viewer_pose(
                            self.poses[i], self.origin
                        ),
                        "raw_intrinsic": self.raw_k[i],
                        "raw_viewer_from_camera_cv": viewer_pose(
                            self.raw_pose[i], self.origin
                        ),
                    }
                )
            records.append(
                {
                    "frame": frame,
                    "timestamp": timestamp,
                    "source": source,
                    "seconds": (timestamp - self.views[0]["timestamp"]) / 1e6,
                    "cameras": camera_rows,
                    "actors": actor_assets,
                    "recipe": recipe,
                    "renders": {m: f"renders/{frame:03d}-{m}.jpg" for m in MODES},
                }
            )
        write_json(self.root / "timeline.json", records)
        return records

    def render(self, timeline):
        static = self.clouds["calibrated_all"]

        def frame(row):
            source = row["source"]
            for mode in MODES:
                actors = dict(
                    np.load(self.root / "actors" / f"{row['frame']:03d}-{mode}.npz")
                )
                image = render_points(
                    combine([static, actors]), self.views[source], (self.h, self.w)
                )
                Image.fromarray(image).save(
                    self.root / row["renders"][mode], quality=94
                )
            if row["frame"] % 25 == 0:
                print(
                    f"RGB point rendering {row['frame'] + 1}/{len(timeline)} (three dynamic modes)",
                    flush=True,
                )

        parallel(frame, timeline, workers=4)


def build(args):
    root = args.output
    if (root / "sweeps-data.json").exists():
        raise ValueError("preserve previous completed reconstruction")
    started = time.perf_counter()
    snapshot = root / "build-source"
    snapshot.mkdir()
    sources = {}
    for name in (
        Path(__file__).name,
        "build_vggt_omega_sequence.py",
        "omega_dynamic.py",
        "omega_densification.py",
        "vggt_common.py",
        "qualify_vggt_omega.py",
        "import_nuscenes_scene.py",
    ):
        path = Path(__file__).with_name(name)
        shutil.copyfile(path, snapshot / name)
        sources[name] = digest(path)
    old_root = root.parent
    old_fence = verify_runtime_sources(old_root)
    write_json(
        root / "sweeps-build-protocol.json",
        {
            "source_hashes": sources,
            "protocol_sha256": digest(root / "sweeps-protocol.json"),
            "inference_sha256": digest(root / "sweeps-inference.json"),
            "old_runtime_fence": old_fence,
            "workers": 8,
            "render_workers": 4,
            "timeline": "all CAM_FRONT exposure timestamps; nearest asynchronous six cameras labeled by actual delta",
            "render": "front camera at its real acquisition time, calibrated_all static cloud plus selected current dynamic mode; white holes; one-pixel point radius",
            "dynamic_visibility_amendment": "before fusion and evaluation: replace the inference protocol's FRONT-only visibility check with nearest exposures in all six directions within 125ms; warp candidate target-clock geometry to each reference's actual exposure via the same instance pose operator for all modes; require finite model depth and at least one visible reference. Reference depth uses calibrated instance candidates at their actual source pixels where available, otherwise calibrated_all depth. No output or heldout score was inspected to choose this change.",
            "single_mode": "one nearest exposure per instance, also moved to the target timestamp; nonrigid actors never combine exposures, but their single-exposure root pose is still an oracle rigid approximation",
            "dynamic_source_visibility": "before temporal fusion, keep the nearest calibrated instance candidate per actual source pixel across all instances; construct reference depth with a nearest-depth reduction, never last-writer ordering",
        },
    )
    reconstruction = Reconstruction(root)
    reconstruction.load()
    reconstruction.colour_scans()
    reconstruction.static("key")
    reconstruction.static("all")
    reconstruction.overlap()
    reconstruction.raw()
    static = reconstruction.score_static()
    reconstruction.actor_candidates()
    dynamic = reconstruction.score_dynamic()
    timeline = reconstruction.timeline()
    reconstruction.render(timeline)
    xyz = (
        reconstruction.clouds["calibrated_all"]["xyz"].astype(float)
        - reconstruction.origin
    ) @ VIEWER_FROM_WORLD.T
    ui = (
        Path(__file__).resolve().parents[2] / "web" / "scripts" / "vggt-omega-sweeps-ui"
    )
    for source in ui.iterdir():
        if source.is_file():
            shutil.copyfile(source, root / source.name)
    write_json(
        root / "sweeps-data.json",
        {
            "scene": reconstruction.scene,
            "input_images": len(reconstruction.views),
            "windows": len(reconstruction.window_stats),
            "frames": len(timeline),
            "seconds": reconstruction.protocol["timestamp_span_s"],
            "inference_seconds": sum(
                w["seconds"] for w in reconstruction.inference["windows"]
            ),
            "grid": [640, 352],
            "checkpoint": "VGGT-Ω 512 · balanced 480",
            "methods": METHODS,
            "assets": reconstruction.assets,
            "cameras": [c for row in timeline for c in row["cameras"]],
            "timeline": timeline,
            "bounds": [np.quantile(xyz, 0.01, axis=0), np.quantile(xyz, 0.99, axis=0)],
            "camera_counts": reconstruction.protocol["camera_counts"],
            "scans": len(reconstruction.scans),
            "permitted_scans": len(reconstruction.static_scans),
            "heldout_scans": len(reconstruction.protocol["heldout_tokens"]),
            "static": static,
            "dynamic": dynamic,
            "instance_tokens": reconstruction.tracks.tokens,
        },
    )
    write_json(
        root / "sweeps-build.json",
        {
            "seconds": time.perf_counter() - started,
            "views": reconstruction.records,
            "windows": reconstruction.window_stats,
            "actor_candidates": reconstruction.actor_stats,
            "counts": {k: a["points"] for k, a in reconstruction.assets.items()},
            "model_source_participation": {
                b: sum(
                    v["calibration"][b]["source_pixels"] > 0
                    for v in reconstruction.records
                )
                for b in ("key", "all")
            },
        },
    )
    print(
        json.dumps(
            {
                "completed": True,
                "seconds": time.perf_counter() - started,
                "counts": {k: a["points"] for k, a in reconstruction.assets.items()},
                "static_metrics": static["metrics"],
                "dynamic": dynamic["summary"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    build(parser.parse_args())
