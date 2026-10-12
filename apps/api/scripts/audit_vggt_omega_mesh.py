"""Check frozen mesh geometry, original textures, masks and per-kind scores."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import minimum_filter

from omega_densification import error_summary
from omega_textured_mesh import mask_faces
from qualify_vggt_omega import digest
from vggt_common import VIEWER_FROM_REFERENCE, verify_runtime_sources, write_json


def audit(output):
    consolidated = verify_runtime_sources(output)
    protocol = json.loads((output / "protocol.json").read_text())
    frozen = json.loads((output / "mesh-protocol.json").read_text())
    amendment_path = output / "mesh-layer-scoring-amendment.json"
    amendment = (
        json.loads(amendment_path.read_text()) if amendment_path.exists() else frozen
    )
    if amendment_path.exists():
        assert amendment["initial_protocol_sha256"] == digest(
            output / "mesh-protocol.json"
        )
        assert amendment["inputs"] == frozen["inputs"]
        assert amendment["initial_mesh_data_sha256"] == digest(
            output / "mesh-initial-all-layer-data.json"
        )
        for name, expected in frozen["source"].items():
            assert digest(output / "mesh-source-initial" / name) == expected
    for name, expected in amendment["source"].items():
        archived = (
            output / "mesh-source-final" / name
            if consolidated
            else Path(__file__).with_name(name)
        )
        assert digest(archived) == expected
    records = json.loads((output / "mesh-data.json").read_text())["runs"]
    dtype = np.dtype([("xyz", "<f4", 3), ("rgb", "u1", 3), ("padding", "u1")])
    rows, errors = [], {}
    textures = set()
    for run in protocol["runs"]:
        root = output / "runs" / run["id"]
        record = next(r for r in records if r["id"] == run["id"])
        for name, expected in frozen["inputs"][run["id"]].items():
            assert digest(root / name) == expected
        for name, expected in amendment.get("geometry", {}).get(run["id"], {}).items():
            assert digest(root / name) == expected
        xyz = np.fromfile(root / "filtered_model.bin", dtype=dtype)["xyz"].astype(float)
        reference = xyz @ VIEWER_FROM_REFERENCE
        with np.load(root / "filtered_model-provenance.npz") as p:
            source, kind, pixel = p["source"], p["kind"], p["pixel"]
        uv_pixels = np.column_stack((pixel % 560, pixel // 560)).astype(float)
        uv = np.fromfile(root / "mesh-uv.bin", dtype="<f4").reshape(-1, 2)
        np.testing.assert_array_equal(
            uv, ((uv_pixels + 0.5) / [560, 320]).astype("<f4")
        )
        triangles = np.fromfile(root / "mesh-triangles.bin", dtype="<u4").reshape(-1, 3)
        assert len(triangles) == record["triangles"]
        assert len(xyz) == record["vertices"]
        assert triangles.max() < len(xyz)
        assert len(np.unique(triangles)) == record["used_vertices"]
        assert np.isfinite(xyz).all()
        with np.load(root / "mesh-accepted-masks.npz") as stored:
            masks = stored["masks"]
        reference_to_global = np.linalg.inv(np.asarray(run["reference_from_global"]))
        edges = np.array([[0, 1], [1, 2], [2, 0]])
        maximum_uv_error, checked, actor_faces = 0.0, 0, 0
        for group in record["groups"]:
            view = run["views"][group["view"]]
            faces = triangles[group["offset"] : group["offset"] + group["triangles"]]
            assert group["offset"] == checked
            checked += len(faces)
            assert np.all(source[faces] == group["view"])
            assert np.all(kind[faces] == group["kind"])
            if group["kind"] == 2:
                assert view["frame"] == run["centre"]
                actor_faces += len(faces)
            path = output / group["texture"]
            assert digest(path) == group["texture_sha256"] == view["sha256"]
            textures.add(str(path))
            camera_from_reference = (
                np.linalg.inv(np.asarray(view["global_from_sensor"]))
                @ reference_to_global
            )
            camera = (
                reference @ camera_from_reference[:3, :3].T
                + camera_from_reference[:3, 3]
            )
            used = np.unique(faces)
            projection = camera[used] @ np.asarray(view["intrinsic"]).T
            maximum_uv_error = max(
                maximum_uv_error,
                float(
                    np.abs(
                        projection[:, :2] / projection[:, 2:] - uv_pixels[used]
                    ).max()
                ),
            )
            z = camera[faces, 2]
            median_z = np.median(z, axis=1)
            pixel_edges = np.linalg.norm(
                uv_pixels[faces[:, edges[:, 0]]] - uv_pixels[faces[:, edges[:, 1]]],
                axis=2,
            )
            world_edges = np.linalg.norm(
                reference[faces[:, edges[:, 0]]] - reference[faces[:, edges[:, 1]]],
                axis=2,
            )
            assert np.all(pixel_edges.max(axis=1) <= 6 + 1e-7)
            assert np.all(
                world_edges.max(axis=1) <= np.maximum(0.35, 0.03 * median_z) + 1e-5
            )
            assert np.all(np.ptp(z, axis=1) <= np.maximum(0.3, 0.03 * median_z) + 1e-5)
            safe = minimum_filter(
                masks[group["view"], group["kind"] - 1].astype(np.uint8),
                size=3,
                mode="constant",
                cval=0,
            ).astype(bool)
            assert mask_faces(uv_pixels, faces, safe).all()
        assert checked == len(triangles) and maximum_uv_error < 0.02
        with np.load(root / "targets.npz") as targets:
            target = {k: targets[k] for k in targets.files}
        expected = np.full(len(target["truth_z"]), np.nan)
        for camera in record["cameras"]:
            prefix = camera["camera"].lower().replace("_", "-")
            for suffix, mask in (("", target["static"]), ("-actors", target["actor"])):
                with np.load(root / f"{prefix}-mesh{suffix}-depth.npz") as saved:
                    depth = saved["depth"]
                selected = (target["view"] == camera["view"]) & mask
                xy = np.rint(target["uv"][selected]).astype(int)
                expected[selected] = depth[xy[:, 1], xy[:, 0]]
        with np.load(root / "mesh-scored-depths.npz") as saved:
            np.testing.assert_array_equal(saved["predicted"], expected)
        for name, mask in (
            ("static", target["static"]),
            ("actors_diagnostic", target["actor"]),
        ):
            assert (
                error_summary(expected, target["truth_z"], mask)
                == record["metrics"][name]
            )
            errors.setdefault((run["window"], name), []).append(
                expected[mask] - target["truth_z"][mask]
            )
        rows.append(
            {
                "id": run["id"],
                "triangles_checked": checked,
                "static_triangles": checked - actor_faces,
                "actor_triangles": actor_faces,
                "maximum_source_pixel_error": maximum_uv_error,
                "vertex_buffer_unchanged": True,
                "all_faces_pass_saved_reconstructed_mask": True,
                "saved_kind_depth_score_consistency": True,
            }
        )
    aggregate = {
        f"{window}frames_{kind}": error_summary(
            np.concatenate(values), np.zeros(sum(len(v) for v in values))
        )
        for (window, kind), values in errors.items()
    }
    result = {
        "runs": rows,
        "original_textures_checked": len(textures),
        "aggregate": aggregate,
        "scoring": "actual mesh pixels, separate static-only and actor-only depth; centre RGB is input",
        "geometry_qualification": "sparse target support only; unobserved surfaces and motion remain unqualified",
    }
    write_json(
        output
        / (
            "mesh-artifact-audit-consolidated.json"
            if consolidated
            else "mesh-artifact-audit.json"
        ),
        result,
    )
    print(
        json.dumps(
            {
                "windows": len(rows),
                "triangles": sum(r["triangles_checked"] for r in rows),
                "textures": len(textures),
                "aggregate": aggregate,
            }
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    audit(parser.parse_args().output)


if __name__ == "__main__":
    main()
