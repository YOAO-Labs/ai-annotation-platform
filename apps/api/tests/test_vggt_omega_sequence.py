"""Meaningful geometry contracts for the complete-scene reconstruction."""

from pathlib import Path
import sys

import numpy as np
from scipy.spatial.transform import Rotation

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_vggt_omega_sequence import (  # noqa: E402
    VIEWER_FROM_WORLD,
    anchor_frames,
    camera_poses,
    references,
    render_points,
    similarity_align,
    viewer_pose,
)


def test_every_window_excludes_all_heldout_acquisitions():
    heldout = {8, 19, 30}
    for frame in range(39):
        selected = anchor_frames(frame, 39, heldout)
        assert selected and not heldout.intersection(selected)
        assert all(abs(i - frame) <= 2 for i in selected)
    for index in range(234):
        other = references(index, 39)
        assert index not in other
        assert len(set(other)) == len(other)
        assert all(0 <= i < 234 for i in other)


def test_global_alignment_recovers_proper_similarity_despite_outliers():
    random = np.random.default_rng(7)
    source = random.normal(size=(60, 3)) * 5
    rotation = Rotation.from_euler("xyz", [0.2, -0.5, 1]).as_matrix()
    translation = np.array([4, -12, 2])
    target = source @ rotation.T * 3 + translation
    target[-3:] += 100
    scale, got_rotation, got_translation, residual, keep = similarity_align(
        source, target
    )
    np.testing.assert_allclose(scale, 3, atol=1e-12)
    np.testing.assert_allclose(got_rotation, rotation, atol=1e-12)
    np.testing.assert_allclose(got_translation, translation, atol=1e-12)
    assert np.linalg.det(got_rotation) > 0.999999
    assert keep.sum() == 57 and np.max(residual[:-3]) < 1e-10


def test_camera_inverse_and_viewer_change_of_basis_preserve_projection():
    r = Rotation.from_euler("xyz", [0.4, 0.1, -0.9]).as_matrix()
    extrinsic = np.column_stack((r, [2, -3, 7]))
    pose = camera_poses(extrinsic[None])[0]
    np.testing.assert_allclose(extrinsic @ pose, np.eye(4)[:3], atol=1e-12)
    origin = np.array([10, 20, 3])
    viewer = viewer_pose(pose, origin)
    camera_point = np.array([1, 2, 5, 1])
    world = pose @ camera_point
    np.testing.assert_allclose(
        (viewer @ camera_point)[:3],
        VIEWER_FROM_WORLD @ (world[:3] - origin),
        atol=1e-12,
    )
    assert np.linalg.det(VIEWER_FROM_WORLD) == 1


def test_saved_point_rendering_depth_tests_and_leaves_holes():
    item = {
        "xyz": np.array([[0, 0, 10], [0, 0, 5], [0, 0, -3], [100, 0, 5]], np.float32),
        "rgb": np.array([[255, 0, 0], [0, 255, 0], [0, 0, 255], [0, 0, 0]], np.uint8),
    }
    view = {
        "global_from_sensor": np.eye(4),
        "intrinsic": np.array([[10.0, 0, 4], [0, 10, 3], [0, 0, 1]]),
    }
    image = render_points(item, view, (7, 9), radius=0)
    np.testing.assert_array_equal(image[3, 4], [0, 255, 0])
    assert np.count_nonzero(np.any(image != 255, axis=2)) == 1
