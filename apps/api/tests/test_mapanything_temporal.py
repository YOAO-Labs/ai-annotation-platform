"""Geometry checks for the read-only temporal MapAnything review."""

from __future__ import annotations

import numpy as np

from scripts.mapanything_temporal import fuse_anchor, project_depth, temporal_support


def _view(depth_value: float = 5, *, valid: bool = True) -> dict:
    depth = np.full((5, 5), depth_value, dtype=np.float32)
    mask = np.full((5, 5), valid, dtype=bool)
    return {
        "depth": depth,
        "mask": mask,
        "rgb": np.full((5, 5, 3), 80, dtype=np.uint8),
        "intrinsic": np.array([[10, 0, 2], [0, 10, 2], [0, 0, 1]], dtype=float),
        "global_from_camera": np.eye(4),
        "prompt_depth": np.zeros((5, 5), dtype=np.float32),
    }


def test_project_depth_uses_relative_camera_pose() -> None:
    source = _view()
    source["mask"][:] = False
    source["mask"][2, 2] = True
    target_from_global = np.eye(4)
    target_from_global[0, 3] = -1
    sy, sx, ty, tx, tz = project_depth(
        source["depth"],
        source["mask"],
        source["intrinsic"],
        source["global_from_camera"],
        target_from_global,
        source["intrinsic"],
        (5, 5),
    )
    assert (sy.tolist(), sx.tolist(), ty.tolist(), tx.tolist()) == ([2], [2], [2], [0])
    assert tz.tolist() == [5]


def test_fusion_fills_only_corroborated_holes_and_rejects_conflicts() -> None:
    views = [_view(), _view(), _view()]
    views[1]["mask"][2, 2] = False
    views[0]["rgb"][2, 2] = (60, 70, 80)
    supported, _ = temporal_support(views)
    depth, mask, rgb, filled = fuse_anchor(views, supported, 1)
    assert filled[2, 2] and mask[2, 2] and depth[2, 2] == 5
    assert rgb[2, 2].tolist() == [60, 70, 80]

    views[0]["rgb"][2, 2] = (10, 20, 30)
    views[2]["rgb"][2, 2] = (10, 20, 30)
    supported, _ = temporal_support(views)
    _, mask, _, filled = fuse_anchor(views, supported, 1)
    assert not filled[2, 2] and not mask[2, 2]
    views[0]["rgb"][2, 2] = (60, 70, 80)
    views[2]["rgb"][2, 2] = (80, 80, 80)

    views[1]["prompt_depth"][2, 2] = 3
    supported, _ = temporal_support(views)
    _, mask, _, filled = fuse_anchor(views, supported, 1)
    assert not filled[2, 2] and not mask[2, 2]

    views[1]["prompt_depth"][2, 2] = 0
    views[1]["prompt_depth"][2, 1] = 3
    supported, _ = temporal_support(views)
    _, mask, _, filled = fuse_anchor(views, supported, 1)
    assert not filled[2, 2] and not mask[2, 2]

    views[1]["prompt_depth"][2, 1] = 0
    views[1]["mask"][2, 2] = True
    views[1]["depth"][2, 2] = 2
    views[1]["prompt_depth"][2, 2] = 0
    supported, _ = temporal_support(views)
    assert not supported[1][2, 2]
    _, mask, _, filled = fuse_anchor(views, supported, 1)
    assert not filled[2, 2] and mask[2, 2]  # Anchor remains visible for comparison.
