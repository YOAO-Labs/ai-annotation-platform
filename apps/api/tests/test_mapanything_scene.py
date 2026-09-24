"""Bounds and spatial selection for the offline full-scene mesh preview."""

from __future__ import annotations

import numpy as np

from scripts.mapanything_scene import SURFACE_VERTEX_DTYPE, assemble_scene_mesh


def _source(path, prefix: str, origin: float, color: int) -> dict:
    xyz = np.array(
        [[origin, 0, 0], [origin + 0.3, 0, 0], [origin, 0.3, 0]], dtype=np.float32
    )
    records = np.zeros(3, dtype=SURFACE_VERTEX_DTYPE)
    records["x"], records["y"], records["z"] = xyz.T
    records["r"] = color
    records.tofile(path / f"{prefix}-vertices.bin")
    np.array([0, 1, 2], dtype="<u4").tofile(path / f"{prefix}-preview.bin")
    return {
        "vertices": f"{prefix}-vertices.bin",
        "preview_indices": f"{prefix}-preview.bin",
        "vertex_count": 3,
        "preview_triangle_count": 1,
    }


def test_scene_mesh_chooses_nearest_observation_per_cell(tmp_path) -> None:
    far = _source(tmp_path, "far", 0, 10)
    near = _source(tmp_path, "near", 0, 220)
    elsewhere = _source(tmp_path, "elsewhere", 2, 120)
    result = assemble_scene_mesh(
        tmp_path,
        "scene",
        [(far, [10, 0, 0]), (near, [0, 0, 1]), (elsewhere, [2, 0, 1])],
    )
    records = np.fromfile(tmp_path / result["vertices"], dtype=SURFACE_VERTEX_DTYPE)
    assert result["source_triangle_count"] == 3
    assert result["triangle_count"] == 2
    assert sorted(set(records["r"].tolist())) == [120, 220]
    assert result["bounds"] == [[0.0, 0.0, 0.0], [2.3, 0.3, 0.0]]
