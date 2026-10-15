"""Metric calibration kernel qualified against the retained Omega experiments.

The production adapter supplies verified current inputs and explicit image transforms.
Offline reproduction scripts remain frozen separately from runtime input policy.
"""

from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree


def transform(points, pose):
    return points @ pose[:3, :3].T + pose[:3, 3]


def project(points, intrinsic, global_from_camera):
    camera = transform(points, np.linalg.inv(global_from_camera))
    homogeneous = camera @ intrinsic.T
    with np.errstate(divide="ignore", invalid="ignore"):
        uv = homogeneous[:, :2] / homogeneous[:, 2:]
    return uv, camera[:, 2]


def unproject(uv, z, intrinsic, global_from_camera):
    rays = np.column_stack((uv, np.ones(len(uv)))) @ np.linalg.inv(intrinsic).T
    return transform(rays * np.asarray(z)[:, None], global_from_camera)


def visible_rows(uv, z, shape):
    """Nearest return per integer pixel, retaining original array identities."""
    h, w = shape
    inside = (
        np.isfinite(uv).all(axis=1)
        & np.isfinite(z)
        & (z >= 1)
        & (z <= 80)
        & (uv[:, 0] >= 0)
        & (uv[:, 0] < w - 1)
        & (uv[:, 1] >= 0)
        & (uv[:, 1] < 0.95 * h)
    )
    rows = np.flatnonzero(inside)
    if not len(rows):
        return rows
    pixels = np.rint(uv[rows]).astype(int)
    order = np.argsort(z[rows], kind="stable")
    _, first = np.unique(pixels[order, 1] * w + pixels[order, 0], return_index=True)
    return rows[order[first]]


def sample_image(image, uv):
    """Bilinear interpolation at integer-centre coordinates, NaN out of bounds."""
    h, w = image.shape[:2]
    valid = (
        np.isfinite(uv).all(axis=1)
        & (uv[:, 0] >= 0)
        & (uv[:, 1] >= 0)
        & (uv[:, 0] < w - 1)
        & (uv[:, 1] < h - 1)
    )
    shape = (len(uv), *image.shape[2:])
    result = np.full(shape, np.nan, dtype=np.float64)
    p = uv[valid]
    x, y = p.astype(int).T
    fx, fy = (p - np.floor(p)).T
    if image.ndim == 3:
        fx, fy = fx[:, None], fy[:, None]
    result[valid] = (
        image[y, x] * (1 - fx) * (1 - fy)
        + image[y, x + 1] * fx * (1 - fy)
        + image[y + 1, x] * (1 - fx) * fy
        + image[y + 1, x + 1] * fx * fy
    )
    return result


def robust_scale(predicted, measured, identities):
    """One median observation per real return, even when visible in many views."""
    good = (
        np.isfinite(predicted)
        & np.isfinite(measured)
        & (predicted > 1e-5)
        & (measured >= 1)
        & (measured <= 80)
    )
    ratios = np.log(measured[good] / predicted[good])
    ids = np.asarray(identities)[good]
    unique, inverse = np.unique(ids, return_inverse=True)
    order = np.argsort(inverse, kind="stable")
    bounds = np.r_[0, np.cumsum(np.bincount(inverse))]
    medians = np.array(
        [np.median(ratios[order[a:b]]) for a, b in zip(bounds[:-1], bounds[1:])]
    )
    if len(medians) < 32:
        raise ValueError("insufficient independent LiDAR anchors")
    middle = np.median(medians)
    mad = np.median(np.abs(medians - middle))
    retained = np.abs(medians - middle) <= max(0.1, 3 * 1.4826 * mad)
    scale = float(np.exp(np.median(medians[retained])))
    return scale, {
        "projected_anchor_observations": int(good.sum()),
        "unique_anchor_returns": len(unique),
        "robust_retained_returns": int(retained.sum()),
        "scale": scale,
        "log_ratio_mad": float(mad),
    }


def local_correction(depth, rgb, uv, measured, radius=24, neighbors=8):
    """Bounded log residual propagation with colour/depth boundary gates."""
    sampled = sample_image(depth, uv)
    colours = sample_image(rgb.astype(float) / 255, uv)
    good = np.isfinite(sampled) & (sampled >= 1) & (sampled <= 80)
    uv, measured, sampled, colours = (
        uv[good],
        measured[good],
        sampled[good],
        colours[good],
    )
    corrected = depth.copy()
    supported = np.zeros(depth.shape, bool)
    if not len(uv):
        return corrected, supported, {"anchors": 0, "locally_supported_pixels": 0}
    residual = np.log(measured / sampled)
    limit = np.log(1.3)
    clipped = np.clip(residual, -limit, limit)
    y, x = np.nonzero(np.isfinite(depth) & (depth >= 1) & (depth <= 80))
    pixels = np.column_stack((x, y))
    tree = cKDTree(uv)
    for first in range(0, len(pixels), 30000):
        p = pixels[first : first + 30000]
        distances, idx = tree.query(p, k=neighbors, distance_upper_bound=radius)
        valid = idx < len(uv)
        idx = np.minimum(idx, len(uv) - 1)
        query_z = depth[p[:, 1], p[:, 0]]
        query_rgb = rgb[p[:, 1], p[:, 0]].astype(float) / 255
        colour_difference = np.linalg.norm(colours[idx] - query_rgb[:, None], axis=2)
        valid &= np.abs(sampled[idx] - query_z[:, None]) <= np.maximum(
            1, 0.15 * query_z[:, None]
        )
        valid &= colour_difference <= 0.2
        weights = np.exp(-0.5 * (distances / (radius / 2)) ** 2)
        weights *= np.exp(-0.5 * (colour_difference / 0.1) ** 2) * valid
        total = weights.sum(axis=1)
        has = (valid.sum(axis=1) >= 2) & (total > 1e-8)
        adjustment = (weights * clipped[idx]).sum(axis=1) / np.maximum(total, 1e-8)
        px, py = p[has].T
        corrected[py, px] *= np.exp(adjustment[has])
        supported[py, px] = True
    return (
        corrected,
        supported,
        {
            "anchors": len(uv),
            "locally_supported_pixels": int(supported.sum()),
            "log_residual_median": float(np.median(residual)),
            "clipped_anchor_fraction": float(np.mean(np.abs(residual) > limit)),
            "near_anchor_count": int((measured < 20).sum()),
            "far_anchor_count": int((measured >= 40).sum()),
        },
    )


def voxel_representatives(points, voxel=0.1):
    """Select real source samples, never average points across a discontinuity."""
    if voxel <= 0 or not np.isfinite(points).all():
        raise ValueError("invalid voxel input")
    _, indices = np.unique(
        np.floor(points / voxel).astype(np.int64), axis=0, return_index=True
    )
    return np.sort(indices)
