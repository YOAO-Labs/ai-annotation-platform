"""Pose-constrained, read-only temporal fusion for MapAnything review meshes."""

from __future__ import annotations

import numpy as np
from scipy.ndimage import distance_transform_edt


def project_depth(
    depth: np.ndarray,
    mask: np.ndarray,
    source_intrinsic: np.ndarray,
    global_from_source: np.ndarray,
    target_from_global: np.ndarray,
    target_intrinsic: np.ndarray,
    target_shape: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Project valid source pixels into a target camera using measured poses."""
    sy, sx = np.nonzero(mask & np.isfinite(depth) & (depth >= 1) & (depth <= 80))
    z = depth[sy, sx]
    camera_xyz = np.stack(
        (
            (sx - source_intrinsic[0, 2]) / source_intrinsic[0, 0] * z,
            (sy - source_intrinsic[1, 2]) / source_intrinsic[1, 1] * z,
            z,
        ),
        axis=1,
    )
    target_from_source = target_from_global @ global_from_source
    target_xyz = camera_xyz @ target_from_source[:3, :3].T + target_from_source[:3, 3]
    tz = target_xyz[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        tx = np.rint(
            target_intrinsic[0, 0] * target_xyz[:, 0] / tz + target_intrinsic[0, 2]
        )
        ty = np.rint(
            target_intrinsic[1, 1] * target_xyz[:, 1] / tz + target_intrinsic[1, 2]
        )
    height, width = target_shape
    inside = (
        np.isfinite(tx)
        & np.isfinite(ty)
        & (tz >= 1)
        & (tz <= 80)
        & (tx >= 0)
        & (tx < width)
        & (ty >= 0)
        & (ty < height)
    )
    return (
        sy[inside],
        sx[inside],
        ty[inside].astype(int),
        tx[inside].astype(int),
        tz[inside],
    )


def consistent_depth(first: np.ndarray, second: np.ndarray) -> np.ndarray:
    return np.abs(first - second) <= np.maximum(0.75, 0.05 * np.minimum(first, second))


def temporal_support(views: list[dict]) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Keep pixels corroborated by another time or their own sparse LiDAR prompt."""
    supported = [np.zeros_like(view["mask"], dtype=bool) for view in views]
    cross_frame = [np.zeros_like(view["mask"], dtype=bool) for view in views]
    for source_index, source in enumerate(views):
        for target_index, target in enumerate(views):
            if source_index == target_index:
                continue
            sy, sx, ty, tx, tz = project_depth(
                source["depth"],
                source["mask"],
                source["intrinsic"],
                source["global_from_camera"],
                np.linalg.inv(target["global_from_camera"]),
                target["intrinsic"],
                target["depth"].shape,
            )
            target_depth = target["depth"][ty, tx]
            matched = target["mask"][ty, tx] & consistent_depth(tz, target_depth)
            cross_frame[source_index][sy[matched], sx[matched]] = True
        prompt = source["prompt_depth"]
        prompt_supported = (prompt > 0) & consistent_depth(source["depth"], prompt)
        supported[source_index] = source["mask"] & (
            cross_frame[source_index] | prompt_supported
        )
    return supported, cross_frame


def fuse_anchor(
    views: list[dict],
    supported: list[np.ndarray],
    anchor_index: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Fill the anchor's invalid pixels with corroborated adjacent observations."""
    anchor = views[anchor_index]
    depth = anchor["depth"].copy()
    rgb = anchor["rgb"].copy()
    # Preserve the anchor observation. Temporal evidence gates only geometry
    # imported from other times; the stricter supported subset is exported
    # separately so coverage and reliability can be compared.
    mask = anchor["mask"].copy()
    filled = np.zeros_like(mask)
    candidates = []
    target_from_global = np.linalg.inv(anchor["global_from_camera"])
    height, width = depth.shape
    if np.any(anchor["prompt_depth"] > 0):
        prompt_distance, prompt_nearest = distance_transform_edt(
            anchor["prompt_depth"] == 0, return_indices=True
        )
    else:
        prompt_distance = np.full(depth.shape, np.inf)
        prompt_nearest = np.zeros((2, height, width), dtype=int)
    raw_valid = anchor["mask"] & np.isfinite(anchor["depth"]) & (anchor["depth"] > 0)
    if np.any(raw_valid):
        raw_distance, raw_nearest = distance_transform_edt(
            ~raw_valid, return_indices=True
        )
    else:
        raw_distance = np.full(depth.shape, np.inf)
        raw_nearest = np.zeros((2, height, width), dtype=int)
    for index, source in enumerate(views):
        if index == anchor_index:
            continue
        sy, sx, ty, tx, tz = project_depth(
            source["depth"],
            supported[index],
            source["intrinsic"],
            source["global_from_camera"],
            target_from_global,
            anchor["intrinsic"],
            depth.shape,
        )
        # An invalid model pixel can still have a LiDAR prompt. Never fill it
        # with geometry that contradicts that measured return.
        prompt = anchor["prompt_depth"][ty, tx]
        accepted = ~anchor["mask"][ty, tx] & (
            (prompt == 0) | consistent_depth(tz, prompt)
        )
        nearby_prompt = anchor["prompt_depth"][
            prompt_nearest[0, ty, tx], prompt_nearest[1, ty, tx]
        ]
        accepted &= (prompt_distance[ty, tx] > 3) | (
            np.abs(tz - nearby_prompt) <= np.maximum(1.0, 0.1 * nearby_prompt)
        )
        nearby_raw = anchor["depth"][raw_nearest[0, ty, tx], raw_nearest[1, ty, tx]]
        accepted &= (raw_distance[ty, tx] > 2) | consistent_depth(tz, nearby_raw)
        source_rgb = source["rgb"][sy, sx].astype(np.float32)
        anchor_rgb = anchor["rgb"][ty, tx].astype(np.float32)
        accepted &= np.abs(source_rgb - anchor_rgb).mean(axis=1) <= 35
        if np.any(accepted):
            candidates.append(
                (
                    ty[accepted] * width + tx[accepted],
                    tz[accepted],
                    source["rgb"][sy[accepted], sx[accepted]],
                )
            )
    if candidates:
        pixels = np.concatenate([item[0] for item in candidates])
        depths = np.concatenate([item[1] for item in candidates])
        colors = np.concatenate([item[2] for item in candidates])
        order = np.lexsort((depths, pixels))
        _, first = np.unique(pixels[order], return_index=True)
        chosen = order[first]
        y, x = np.divmod(pixels[chosen], width)
        depth[y, x] = depths[chosen]
        rgb[y, x] = colors[chosen]
        mask[y, x] = True
        filled[y, x] = True
    return depth, mask, rgb, filled
