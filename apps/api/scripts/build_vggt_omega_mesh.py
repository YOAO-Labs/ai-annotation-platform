"""Add a textured surface renderer to the frozen Omega point-cloud pilot."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import numpy as np
from PIL import Image

from build_vggt_omega_review import actor_mask, anchors_for_view
from omega_densification import (
    error_summary,
    lidar_evidence,
    project,
    resized_intrinsic,
    transform,
)
from omega_textured_mesh import render_mesh, triangulate
from qualify_vggt_omega import PARAMETERS, SceneData, digest
from vggt_common import (
    VIEWER_FROM_REFERENCE,
    cross_view_support,
    points_in_actors,
    write_json,
)

MESH_PARAMETERS = {
    "maximum_source_edge_px": 6,
    "maximum_depth_jump_m": 0.3,
    "maximum_depth_jump_relative": 0.03,
    "maximum_world_edge_m": 0.35,
    "maximum_world_edge_relative": 0.03,
    "accepted_mask_erosion_px": 1,
    "vertices": "exactly reuse filtered_model.bin; no position optimization",
    "texture_uv": "top-down ((u+0.5)/560,(v+0.5)/320); full original RGB",
    "raster": "double-sided; discard faces with any camera-Z <=0.05m; perspective correct 1/Z",
    "scoring": "actual mesh pixel coverage; camera-Z; no 1.5px point footprint",
    "scoring_layers": "static-only mesh for static targets; actor-only mesh for actor diagnostics",
}


def restore_masks(data, run, corrected, valid):
    views = run["views"]
    actors = [actor_mask(data, run["scene"], v) for v in views]
    pieces, ids = [], []
    for scan in run["scans"]:
        if scan["token"] not in run["anchor_scans"]:
            continue
        xyz, row = data.scan(run["scene"], scan["frame"])
        static = ~points_in_actors(
            xyz,
            data.boxes(run["scene"], row["timestamp"]),
            PARAMETERS["actor_padding_m"],
        )
        rows = np.flatnonzero(static)
        pieces.append(xyz[rows])
        ids.append(np.array([f"{row['token']}:{r}" for r in rows]))
    points, identities = np.concatenate(pieces), np.concatenate(ids)
    masks = []
    for i, view in enumerate(views):
        eligible = valid[i] & ~actors[i]
        refs = [
            {
                "intrinsic": np.asarray(v["intrinsic"]),
                "reference_from_camera": np.asarray(v["global_from_sensor"]),
                "model": np.where(valid[j] & ~actors[j], corrected[j], np.nan),
            }
            for j, v in enumerate(views)
            if v["camera"] == view["camera"] and v["frame"] != view["frame"]
        ]
        support, conflict = cross_view_support(
            np.where(eligible, corrected[i], np.nan),
            np.asarray(view["intrinsic"]),
            np.asarray(view["global_from_sensor"]),
            refs,
            0.4,
            0.03,
            2,
        )
        anchor = anchors_for_view(
            points,
            identities,
            np.asarray(view["intrinsic"]),
            np.asarray(view["global_from_sensor"]),
            actors[i],
        )
        direct, veto = lidar_evidence(
            corrected[i], anchor["uv"], anchor["z"], 0.3, 0.03
        )
        masks.append(
            (
                eligible & ((support >= 1) | direct) & (conflict == 0) & ~veto,
                valid[i] & actors[i] & (view["frame"] == run["centre"]),
            )
        )
    return masks


def build_run(output, data, run):
    start = time.perf_counter()
    directory = output / "runs" / run["id"]
    dtype = np.dtype([("xyz", "<f4", 3), ("rgb", "u1", 3), ("padding", "u1")])
    points = np.fromfile(directory / "filtered_model.bin", dtype=dtype)
    reference = points["xyz"].astype(float) @ VIEWER_FROM_REFERENCE
    global_from_reference = np.linalg.inv(np.asarray(run["reference_from_global"]))
    global_xyz = transform(reference, global_from_reference)
    with np.load(directory / "filtered_model-provenance.npz") as cached:
        source, pixel, kinds = (
            cached["source"].copy(),
            cached["pixel"].copy(),
            cached["kind"].copy(),
        )
    with np.load(directory / "corrected-depths.npz") as cached:
        corrected, valid = cached["corrected"].copy(), cached["valid"].copy()
    masks = restore_masks(data, run, corrected, valid)
    uv = np.column_stack((pixel % 560, pixel // 560)).astype(float)
    texture_uv = ((uv + 0.5) / [560, 320]).astype("<f4")
    texture_uv.tofile(directory / "mesh-uv.bin")
    all_faces, groups, render_groups = [], [], []
    layer_groups = {1: [], 2: []}
    offset = 0
    camera_rows = []
    for i, view in enumerate(run["views"]):
        texture_path = output / "textures" / f"{view['token']}.jpg"
        if not texture_path.exists():
            if digest(view["path"]) != view["sha256"]:
                raise ValueError("original texture changed")
            shutil.copy2(view["path"], texture_path)
        if digest(texture_path) != view["sha256"]:
            raise ValueError("copied RGB is not the original texture")
        texture = np.asarray(Image.open(texture_path).convert("RGB"))
        for kind in (1, 2):
            selected = np.flatnonzero((source == i) & (kinds == kind))
            accepted = masks[i][kind - 1]
            if not accepted[
                uv[selected, 1].astype(int), uv[selected, 0].astype(int)
            ].all():
                raise ValueError("restored mask differs from frozen retained points")
            _, depth = project(
                global_xyz[selected],
                np.asarray(view["intrinsic"]),
                np.asarray(view["global_from_sensor"]),
            )
            local_faces, stats = triangulate(
                uv[selected], reference[selected], depth, accepted
            )
            faces = selected[local_faces].astype("<u4")
            if not len(faces):
                continue
            all_faces.append(faces)
            groups.append(
                {
                    "view": i,
                    "kind": kind,
                    "offset": offset,
                    "triangles": len(faces),
                    "texture": f"textures/{view['token']}.jpg",
                    "texture_sha256": view["sha256"],
                    "statistics": stats,
                }
            )
            render_groups.append((faces, texture))
            layer_groups[kind].append((faces, texture))
            offset += len(faces)
        if view["frame"] == run["centre"]:
            pose = np.asarray(run["reference_from_global"]) @ np.asarray(
                view["global_from_sensor"]
            )
            viewer_from_camera = np.eye(4)
            viewer_from_camera[:3, :3] = VIEWER_FROM_REFERENCE @ pose[:3, :3]
            viewer_from_camera[:3, 3] = VIEWER_FROM_REFERENCE @ pose[:3, 3]
            camera_rows.append(
                {
                    "camera": view["camera"],
                    "intrinsic": view["intrinsic"],
                    "size": [560, 320],
                    "viewer_from_camera_cv": viewer_from_camera.tolist(),
                    "view": i,
                }
            )
    indices = np.concatenate(all_faces)
    indices.tofile(directory / "mesh-triangles.bin")
    np.savez_compressed(directory / "mesh-accepted-masks.npz", masks=np.asarray(masks))
    with np.load(directory / "targets.npz") as targets:
        target = {k: targets[k].copy() for k in targets.files}
    predicted = np.full(len(target["truth_z"]), np.nan)
    renders = []
    for camera in camera_rows:
        i = camera["view"]
        view = run["views"][i]
        camera_from_reference = (
            np.linalg.inv(np.asarray(view["global_from_sensor"]))
            @ global_from_reference
        )
        prefix = view["camera"].lower().replace("_", "-")
        full_k = resized_intrinsic(
            np.asarray(view["intrinsic"]), (560, 320), (800, 450)
        )
        render_row = {"camera": view["camera"]}
        for label, draw_groups, suffix in (
            ("static", layer_groups[1], ""),
            ("actors", layer_groups[2], "-actors"),
            ("all", render_groups, "-all"),
        ):
            rgba, depth = render_mesh(
                reference,
                texture_uv,
                draw_groups,
                np.asarray(view["intrinsic"]),
                camera_from_reference,
                (560, 320),
            )
            Image.fromarray(rgba).save(directory / f"{prefix}-mesh{suffix}.png")
            np.savez_compressed(
                directory / f"{prefix}-mesh{suffix}-depth.npz",
                depth=np.where(np.isfinite(depth), depth, np.nan),
            )
            if label != "all":
                rows = (target["view"] == i) & target[
                    "static" if label == "static" else "actor"
                ]
                # Score only the corresponding kind, with its actual covered pixel.
                xy = np.rint(target["uv"][rows]).astype(int)
                samples = depth[xy[:, 1], xy[:, 0]]
                predicted[rows] = np.where(np.isfinite(samples), samples, np.nan)
            rgb_large, _ = render_mesh(
                reference,
                texture_uv,
                draw_groups,
                full_k,
                camera_from_reference,
                (800, 450),
            )
            Image.fromarray(rgb_large).save(
                directory / f"{prefix}-mesh{suffix}-rgb.png"
            )
            key = "" if label == "static" else f"{label}_"
            render_row[f"{key}render"] = (
                f"runs/{run['id']}/{prefix}-mesh{suffix}-rgb.png"
            )
            render_row[f"{key}grid_render"] = (
                f"runs/{run['id']}/{prefix}-mesh{suffix}.png"
            )
        renders.append(render_row)
    # A displaced view has no independent RGB truth; it is explicitly a visual probe.
    front = next(c for c in camera_rows if c["camera"] == "CAM_FRONT")
    view = run["views"][front["view"]]
    displaced_pose = np.asarray(view["global_from_sensor"]).copy()
    displaced_pose[:3, 3] += displaced_pose[:3, 0] * 0.5
    rgba, _ = render_mesh(
        reference,
        texture_uv,
        layer_groups[1],
        resized_intrinsic(np.asarray(view["intrinsic"]), (560, 320), (800, 450)),
        np.linalg.inv(displaced_pose) @ global_from_reference,
        (800, 450),
    )
    Image.fromarray(rgba).save(directory / "novel-front-right-0.5m.png")
    np.savez_compressed(directory / "mesh-scored-depths.npz", predicted=predicted)
    record = {
        "id": run["id"],
        "vertices": len(points),
        "used_vertices": len(np.unique(indices)),
        "triangles": len(indices),
        "point_source_sha256": digest(directory / "filtered_model.bin"),
        "provenance_sha256": digest(directory / "filtered_model-provenance.npz"),
        "indices": f"runs/{run['id']}/mesh-triangles.bin",
        "uv": f"runs/{run['id']}/mesh-uv.bin",
        "vertex_buffer": f"runs/{run['id']}/filtered_model.bin",
        "groups": groups,
        "cameras": camera_rows,
        "renders": renders,
        "novel_render": f"runs/{run['id']}/novel-front-right-0.5m.png",
        "metrics": {
            "static": error_summary(predicted, target["truth_z"], target["static"]),
            "actors_diagnostic": error_summary(
                predicted, target["truth_z"], target["actor"]
            ),
        },
        "seconds": time.perf_counter() - start,
    }
    write_json(directory / "mesh.json", record)
    print(
        json.dumps(
            {
                "id": run["id"],
                "triangles": len(indices),
                "static": record["metrics"]["static"],
                "seconds": record["seconds"],
            }
        ),
        flush=True,
    )
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repair-layer-scoring", action="store_true")
    args = parser.parse_args()
    output = args.output
    protocol = json.loads((output / "protocol.json").read_text())
    mesh_protocol = {
        "parameters": MESH_PARAMETERS,
        "frozen_before_mesh_scoring": True,
        "parent_protocol_sha256": digest(output / "protocol.json"),
        "inputs": {
            r["id"]: {
                name: digest(output / "runs" / r["id"] / name)
                for name in (
                    "filtered_model.bin",
                    "filtered_model-provenance.npz",
                    "corrected-depths.npz",
                )
            }
            for r in protocol["runs"]
        },
        "source": {
            p.name: digest(p)
            for p in (
                Path(__file__),
                Path(__file__).with_name("omega_textured_mesh.py"),
            )
        },
    }
    if args.repair_layer_scoring:
        previous = json.loads((output / "mesh-protocol.json").read_text())
        if previous["inputs"] != mesh_protocol["inputs"]:
            raise ValueError("frozen mesh inputs changed")
        amendment = output / "mesh-layer-scoring-amendment.json"
        if amendment.exists():
            raise ValueError("layer-scoring amendment already exists")
        mesh_protocol["reason"] = (
            "Fix initial all-layer depth used for separate static/actor metrics. "
            "Render and score each kind separately; keep all-layer occlusion previews. "
            "No threshold, input, vertex, or triangle changes."
        )
        mesh_protocol["initial_protocol_sha256"] = digest(output / "mesh-protocol.json")
        mesh_protocol["initial_mesh_data_sha256"] = digest(output / "mesh-data.json")
        mesh_protocol["geometry"] = {
            r["id"]: {
                name: digest(output / "runs" / r["id"] / name)
                for name in (
                    "mesh-uv.bin",
                    "mesh-triangles.bin",
                    "targets.npz",
                )
            }
            for r in protocol["runs"]
        }
        shutil.copy2(
            output / "mesh-data.json", output / "mesh-initial-all-layer-data.json"
        )
        write_json(amendment, mesh_protocol)
    elif (output / "mesh-protocol.json").exists():
        raise ValueError("mesh protocol already exists; refuse a silent rerun")
    else:
        write_json(output / "mesh-protocol.json", mesh_protocol)
    (output / "textures").mkdir(exist_ok=True)
    data = SceneData(Path(protocol["data_root"]))
    records = [build_run(output, data, run) for run in protocol["runs"]]
    if args.repair_layer_scoring:
        for identity, hashes in mesh_protocol["geometry"].items():
            for name, expected in hashes.items():
                if digest(output / "runs" / identity / name) != expected:
                    raise ValueError(
                        f"layer-scoring repair changed geometry: {identity}/{name}"
                    )
    write_json(
        output / "mesh-data.json", {"parameters": MESH_PARAMETERS, "runs": records}
    )
    original = json.loads((output / "data.json").read_text())
    for run in original["runs"]:
        run["mesh"] = next(r for r in records if r["id"] == run["id"])
    write_json(output / "data.json", original)


if __name__ == "__main__":
    main()
