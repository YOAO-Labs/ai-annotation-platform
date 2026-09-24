"""Bounded scene mesh assembly for the offline MapAnything sequence review."""

from __future__ import annotations

from pathlib import Path

import numpy as np

SURFACE_VERTEX_DTYPE = np.dtype(
    [
        ("x", "<f4"),
        ("y", "<f4"),
        ("z", "<f4"),
        ("r", "u1"),
        ("g", "u1"),
        ("b", "u1"),
        ("padding", "u1"),
    ]
)


def assemble_scene_mesh(
    output: Path,
    prefix: str,
    sources: list[tuple[dict, list[float]]],
    *,
    voxel_m: float = 0.5,
    max_triangles: int = 250_000,
    max_preview_triangles: int = 30_000,
) -> dict:
    """Keep the nearest observed triangle per spatial/orientation cell."""
    if voxel_m <= 0 or max_triangles < 1 or max_preview_triangles < 1:
        raise ValueError("scene mesh bounds must be positive")
    positions = []
    colors = []
    distances = []
    for surface, camera_position in sources:
        records = np.fromfile(output / surface["vertices"], dtype=SURFACE_VERTEX_DTYPE)
        faces = np.fromfile(output / surface["preview_indices"], dtype="<u4")
        if (
            len(records) != surface["vertex_count"]
            or len(faces) != surface["preview_triangle_count"] * 3
        ):
            raise ValueError("surface buffer length does not match metadata")
        if not len(faces):
            continue
        faces = faces.reshape(-1, 3)
        if int(faces.max()) >= len(records):
            raise ValueError("surface index exceeds vertex count")
        xyz = np.stack((records["x"], records["y"], records["z"]), axis=1)
        rgb = np.stack((records["r"], records["g"], records["b"]), axis=1)
        triangles = xyz[faces]
        triangle_colors = rgb[faces]
        normals = np.cross(
            triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
        )
        centers = triangles.mean(axis=1)
        valid = np.isfinite(triangles).all(axis=(1, 2)) & (
            np.linalg.norm(normals, axis=1) > 1e-5
        )
        if not np.any(valid):
            continue
        triangles = triangles[valid]
        triangle_colors = triangle_colors[valid]
        centers = centers[valid]
        normals = normals[valid]
        dominant_axis = np.abs(normals).argmax(axis=1)
        orientation = dominant_axis * 2 + (
            normals[np.arange(len(normals)), dominant_axis] >= 0
        )
        cells = np.floor(centers / voxel_m).astype(np.int32)
        positions.append(triangles)
        colors.append(triangle_colors)
        distances.append(
            (
                np.column_stack((cells, orientation)),
                np.linalg.norm(centers - np.asarray(camera_position), axis=1),
            )
        )
    if not positions:
        raise ValueError("no valid scene triangles")
    triangles = np.concatenate(positions)
    rgb = np.concatenate(colors)
    keys = np.concatenate([item[0] for item in distances])
    distance = np.concatenate([item[1] for item in distances])
    order = np.lexsort((distance, keys[:, 3], keys[:, 2], keys[:, 1], keys[:, 0]))
    sorted_keys = keys[order]
    keep = np.empty(len(order), dtype=bool)
    keep[0] = True
    keep[1:] = np.any(sorted_keys[1:] != sorted_keys[:-1], axis=1)
    chosen = order[keep]
    deduplicated_count = len(chosen)
    if len(chosen) > max_triangles:
        chosen = chosen[np.linspace(0, len(chosen) - 1, max_triangles, dtype=int)]
    selected_xyz = triangles[chosen].reshape(-1, 3)
    selected_rgb = rgb[chosen].reshape(-1, 3)
    records = np.empty(len(selected_xyz), dtype=SURFACE_VERTEX_DTYPE)
    records["x"], records["y"], records["z"] = selected_xyz.T
    records["r"], records["g"], records["b"] = selected_rgb.T
    records["padding"] = 0
    vertex_file = f"{prefix}-vertices.bin"
    index_file = f"{prefix}-indices.bin"
    preview_file = f"{prefix}-preview-indices.bin"
    records.tofile(output / vertex_file)
    indices = np.arange(len(selected_xyz), dtype="<u4").reshape(-1, 3)
    indices.tofile(output / index_file)
    stride = max(1, int(np.ceil(len(indices) / max_preview_triangles)))
    indices[::stride].tofile(output / preview_file)
    return {
        "vertices": vertex_file,
        "indices": index_file,
        "preview_indices": preview_file,
        "vertex_count": len(records),
        "triangle_count": len(indices),
        "preview_triangle_count": len(indices[::stride]),
        "source_triangle_count": len(triangles),
        "deduplicated_triangle_count": deduplicated_count,
        "voxel_m": voxel_m,
        "bounds": [
            [round(float(value), 3) for value in selected_xyz.min(axis=0)],
            [round(float(value), 3) for value in selected_xyz.max(axis=0)],
        ],
    }
