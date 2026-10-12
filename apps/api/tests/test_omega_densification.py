"""Contracts for metric correction and acquisition-separated geometry."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from omega_densification import (  # noqa: E402
    acquisition_split,
    cloud_depth_at_targets,
    error_summary,
    lidar_evidence,
    local_correction,
    project,
    resized_intrinsic,
    robust_scale,
    sample_image,
    unproject,
    visible_rows,
    voxel_representatives,
)
from vggt_common import cross_view_support  # noqa: E402


def test_pil_pixel_centres_and_metric_round_trip():
    original = np.array([[1000, 0, 799.5], [0, 1000, 449.5], [0, 0, 1]])
    k = resized_intrinsic(original, (1600, 900), (560, 320))
    np.testing.assert_allclose(k[:2, 2], [279.5, 159.5])
    pose = np.eye(4)
    pose[:3, 3] = [4, 8, -3]
    uv = np.array([[279.5, 159.5], [58, 37], [490, 210]])
    z = np.array([5, 10, 30])
    xyz = unproject(uv, z, k, pose)
    got_uv, got_z = project(xyz, k, pose)
    np.testing.assert_allclose(got_uv, uv, atol=1e-10)
    np.testing.assert_allclose(got_z, z)


def test_entire_scan_excluded_including_all_rows():
    assert acquisition_split(["before", "centre", "after"], "centre") == [
        "before",
        "after",
    ]
    with pytest.raises(ValueError):
        acquisition_split(["centre", "centre"], "centre")
    with pytest.raises(ValueError):
        acquisition_split(["centre"], "centre")


def test_scale_deduplicates_multiview_returns_and_rejects_outliers():
    ids = np.arange(100).astype(str)
    predicted = np.ones(100)
    truth = np.full(100, 3.0)
    truth[-4:] = 40
    # One anomalous return has many duplicated camera observations.
    result, stats = robust_scale(
        np.r_[predicted, np.ones(1000)],
        np.r_[truth, np.full(1000, 60)],
        np.r_[ids, np.full(1000, "duplicate")],
    )
    assert result == pytest.approx(3)
    assert stats["unique_anchor_returns"] == 101
    assert stats["robust_retained_returns"] == 96


def test_local_residual_stays_within_colour_depth_and_radius_gates():
    depth = np.full((30, 100), 10, np.float32)
    depth[:, 30:60] = 30
    rgb = np.zeros((30, 100, 3), np.uint8)
    rgb[:, 60:] = 255
    uv = np.array([[12.0, 14], [13, 14], [14, 14]])
    corrected, supported, _ = local_correction(depth, rgb, uv, np.full(3, 12))
    assert corrected[14, 13] == pytest.approx(12)
    assert supported[14, 13]
    np.testing.assert_array_equal(corrected[:, 30:], depth[:, 30:])
    assert not supported[:, 60:].any()
    assert not supported[:, 90:].any()


def test_visibility_keeps_nearest_raw_identity_and_no_ego_pixels():
    pixels = np.array([[4, 4], [4.1, 4.1], [8, 29], [8, 10]])
    rows = visible_rows(pixels, np.array([15, 5, 2, -1]), (30, 30))
    assert rows.tolist() == [1]
    image = np.arange(16).reshape(4, 4)
    assert sample_image(image, np.array([[1.5, 1.5]]))[0] == pytest.approx(7.5)


def test_voxel_preserves_original_positions_and_source_rows():
    points = np.array([[0.01, 0.01, 0.01], [0.09, 0.09, 0.09], [0.11, 0.01, 0.01]])
    indices = voxel_representatives(points)
    np.testing.assert_array_equal(indices, [0, 2])
    np.testing.assert_array_equal(points[indices], points[[0, 2]])


def test_missing_coverage_is_penalized_on_fixed_targets():
    metrics = error_summary(np.array([10.1, np.nan, 13]), np.full(3, 10))
    assert metrics["coverage"] == pytest.approx(2 / 3)
    assert metrics["correct_coverage_0.5m"] == pytest.approx(1 / 3)
    assert metrics["mae_m"] == pytest.approx(1.55)


def test_lidar_veto_cannot_be_overridden_by_correlated_model_support():
    depth = np.full((10, 10), 10.0)
    uv = np.array([[3, 3], [4, 4], [5, 5]], float)
    direct, conflict = lidar_evidence(depth, uv, np.array([10.1, 15, 5]))
    assert direct[3, 3] and not conflict[3, 3]
    assert conflict[4, 4] and not direct[4, 4]
    # Occlusion by a nearer measured point is not a free-space contradiction.
    assert not conflict[5, 5] and not direct[5, 5]
    support = np.ones(depth.shape, bool)
    accepted = support & ~conflict
    assert not accepted[4, 4]
    assert not conflict[4, 3]  # No invented dense LiDAR support.


def test_actual_cloud_footprint_is_finite_and_reprojection_agreement_is_occlusion_aware():
    k, pose = np.eye(3), np.eye(4)
    xyz = unproject(np.array([[4.0, 4.0]]), np.array([10]), k, pose)
    predictions = cloud_depth_at_targets(
        xyz, np.array([[4.0, 4], [7, 4]]), k, pose, (20, 20)
    )
    assert predictions[0] == pytest.approx(10)
    assert np.isnan(predictions[1])
    depth = np.full((20, 20), np.nan)
    depth[4, 4] = 10
    reference = {"intrinsic": k, "reference_from_camera": pose, "model": depth.copy()}
    support, conflict = cross_view_support(depth, k, pose, [reference], 0.4, 0.03, 2)
    assert support[4, 4] == 1 and conflict[4, 4] == 0
    reference["model"][4, 4] = 5
    support, conflict = cross_view_support(depth, k, pose, [reference], 0.4, 0.03, 2)
    assert support[4, 4] == 0 and conflict[4, 4] == 0
    reference["model"][4, 4] = 15
    support, conflict = cross_view_support(depth, k, pose, [reference], 0.4, 0.03, 2)
    assert support[4, 4] == 0 and conflict[4, 4] == 1
