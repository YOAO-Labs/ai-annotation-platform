"""Conservative source-image triangulation and perspective-correct CPU rendering."""

from __future__ import annotations

import numpy as np
from numba import njit
from scipy.ndimage import minimum_filter
from scipy.spatial import Delaunay


@njit(cache=False)
def mask_faces(uv, faces, mask):
    keep = np.ones(len(faces), np.bool_)
    height, width = mask.shape
    for i in range(len(faces)):
        a, b, c = faces[i]
        ax, ay = uv[a]
        bx, by = uv[b]
        cx, cy = uv[c]
        for vertex in (a, b, c):
            x, y = int(uv[vertex, 0]), int(uv[vertex, 1])
            if x < 0 or x >= width or y < 0 or y >= height or not mask[y, x]:
                keep[i] = False
        if not keep[i]:
            continue
        denominator = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(denominator) < 1e-8:
            keep[i] = False
            continue
        for y in range(
            max(0, int(np.floor(min(ay, by, cy)))),
            min(height, int(np.ceil(max(ay, by, cy))) + 1),
        ):
            for x in range(
                max(0, int(np.floor(min(ax, bx, cx)))),
                min(width, int(np.ceil(max(ax, bx, cx))) + 1),
            ):
                wa = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / denominator
                wb = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / denominator
                if (
                    wa >= -1e-7
                    and wb >= -1e-7
                    and wa + wb <= 1 + 1e-7
                    and not mask[y, x]
                ):
                    keep[i] = False
    return keep


def triangulate(uv, xyz, depth, accepted, pixel_edge=6.0):
    if len(uv) < 3 or np.linalg.matrix_rank(uv - uv.mean(axis=0)) < 2:
        return np.empty((0, 3), np.int32), {"delaunay": 0, "accepted": 0}
    faces = Delaunay(uv).simplices
    count = len(faces)
    edges = np.array([[0, 1], [1, 2], [2, 0]])
    pixel_lengths = np.linalg.norm(
        uv[faces[:, edges[:, 0]]] - uv[faces[:, edges[:, 1]]], axis=2
    )
    world_lengths = np.linalg.norm(
        xyz[faces[:, edges[:, 0]]] - xyz[faces[:, edges[:, 1]]], axis=2
    )
    z = depth[faces]
    median = np.median(z, axis=1)
    geometric = (
        (pixel_lengths.max(axis=1) <= pixel_edge)
        & (world_lengths.max(axis=1) <= np.maximum(0.35, 0.03 * median))
        & (np.ptp(z, axis=1) <= np.maximum(0.3, 0.03 * median))
    )
    faces = faces[geometric]
    # Conservatively cover fractional UV boundary samples as well as pixel centres.
    safe = minimum_filter(
        accepted.astype(np.uint8), size=3, mode="constant", cval=0
    ).astype(bool)
    faces = faces[mask_faces(uv.astype(float), faces, safe)]
    return faces.astype(np.int32), {
        "delaunay": count,
        "geometry_passed": int(geometric.sum()),
        "accepted": len(faces),
    }


@njit(cache=False)
def rasterize(screen, z, uv, faces, texture, rgb, depth):
    """Array integer centres; inverse-Z interpolation supplies metric camera-Z."""
    height, width = depth.shape
    th, tw = texture.shape[:2]
    for i in range(len(faces)):
        a, b, c = faces[i]
        za, zb, zc = z[a], z[b], z[c]
        if min(za, zb, zc) <= 0.05:
            continue
        ax, ay = screen[a]
        bx, by = screen[b]
        cx, cy = screen[c]
        denominator = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(denominator) < 1e-8:
            continue
        left = max(0, int(np.ceil(min(ax, bx, cx))))
        right = min(width - 1, int(np.floor(max(ax, bx, cx))))
        top = max(0, int(np.ceil(min(ay, by, cy))))
        bottom = min(height - 1, int(np.floor(max(ay, by, cy))))
        if left > right or top > bottom:
            continue
        for y in range(top, bottom + 1):
            for x in range(left, right + 1):
                wa = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / denominator
                wb = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / denominator
                wc = 1 - wa - wb
                if min(wa, wb, wc) < -1e-7:
                    continue
                inverse_z = wa / za + wb / zb + wc / zc
                predicted = 1 / inverse_z
                if predicted >= depth[y, x]:
                    continue
                u = (
                    wa * uv[a, 0] / za + wb * uv[b, 0] / zb + wc * uv[c, 0] / zc
                ) / inverse_z
                v = (
                    wa * uv[a, 1] / za + wb * uv[b, 1] / zb + wc * uv[c, 1] / zc
                ) / inverse_z
                tx = min(tw - 1.000001, max(0.0, u * tw - 0.5))
                ty = min(th - 1.000001, max(0.0, v * th - 0.5))
                ix, iy = int(tx), int(ty)
                fx, fy = tx - ix, ty - iy
                for channel in range(3):
                    value = (
                        texture[iy, ix, channel] * (1 - fx) * (1 - fy)
                        + texture[iy, ix + 1, channel] * fx * (1 - fy)
                        + texture[iy + 1, ix, channel] * (1 - fx) * fy
                        + texture[iy + 1, ix + 1, channel] * fx * fy
                    )
                    rgb[y, x, channel] = min(255, max(0, int(value + 0.5)))
                rgb[y, x, 3] = 255
                depth[y, x] = predicted


def render_mesh(xyz, uv, groups, intrinsic, camera_from_reference, size):
    width, height = size
    camera = xyz @ camera_from_reference[:3, :3].T + camera_from_reference[:3, 3]
    homogeneous = camera @ intrinsic.T
    screen = homogeneous[:, :2] / np.maximum(camera[:, 2:], 1e-12)
    rgb = np.zeros((height, width, 4), np.uint8)
    depth = np.full((height, width), np.inf, np.float32)
    for faces, texture in groups:
        rasterize(screen, camera[:, 2], uv, faces, texture, rgb, depth)
    return rgb, depth
