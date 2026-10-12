"""CPU-only contracts for scene ownership and timestamped object motion."""

import sys
from pathlib import Path

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from omega_densification import voxel_representatives
from omega_dynamic import (
    ActorTracks,
    assign_instances,
    fast_voxel,
    overlapping_windows,
    warp_instance,
)
from build_vggt_omega_sweeps import Reconstruction, allowed_scans


def box(token="actor", time=0, translation=(0, 0, 0), angle=0, size=(2, 4, 2)):
    q = Rotation.from_euler("z", angle).as_quat()
    return {
        "instance_token": token,
        "timestamp": time,
        "translation": list(translation),
        "rotation": q[[3, 0, 1, 2]].tolist(),
        "rotation_matrix": Rotation.from_euler("z", angle).as_matrix(),
        "size": list(size),
        "category": "vehicle.car",
    }


def test_windows_cover_all_exposures_exactly_once_as_owned_outputs():
    windows, owner = overlapping_windows(1318)
    assert len(windows) == 18
    assert all(len(w) == 144 for w in windows)
    assert windows[-1][-1] == 1317
    assert set(np.concatenate(windows)) == set(range(1318))
    assert all(i in windows[own] for i, own in enumerate(owner))
    assert sum(int((owner == i).sum()) for i in range(len(windows))) == 1318
    assert owner[107] == 0 and owner[108] == 1


def test_voxel_keeps_real_first_samples_without_hash_collisions_or_averages():
    points = np.array(
        [
            [0.09, 0, 0],
            [0.01, 0, 0],
            [-0.01, 0, 0],
            [0, -0.01, 0],
            [0, 0, -0.01],
            [1000, -2000, 3],
        ],
        np.float32,
    )
    assert np.array_equal(fast_voxel(points), voxel_representatives(points))
    assert np.array_equal(fast_voxel(points), [0, 2, 3, 4, 5])
    with pytest.raises(ValueError):
        fast_voxel(np.array([[np.inf, 0, 0]]))


def test_strict_motion_rejects_endpoint_extrapolation_and_large_annotation_gaps():
    tracks = ActorTracks([[box(time=100000), box(time=600000, translation=(1, 0, 0))]])
    assert tracks.box("actor", 99999) is None
    assert tracks.box("actor", 600001) is None
    assert np.allclose(tracks.box("actor", 350000)["translation"], [0.5, 0, 0])
    assert tracks.box("actor", 100000) is not None
    assert ActorTracks([[box()]]).box("actor", 0) is None
    assert ActorTracks([[box(), box(time=800000)]]).box("actor", 400000) is None


def test_rigid_warp_preserves_metric_offsets_and_instance_identity():
    source = box(translation=(10, 20, 0), size=(2, 4, 2))
    target = box(translation=(11, 21, 0), angle=np.pi / 2, size=(8, 16, 9))
    points = np.array([[11, 20, 0], [10, 21, 1]])
    moved = warp_instance(points, source, target)
    assert np.allclose(moved, [[11, 22, 0], [10, 21, 1]])
    assert np.allclose(warp_instance(moved, target, source), points)
    with pytest.raises(ValueError):
        warp_instance(points, source, box(token="different"))


def test_actor_membership_requires_unique_identity_and_correct_nuscenes_axes():
    boxes = [box(), box(token="other", translation=(3, 0, 0))]
    labels = assign_instances(
        np.array([[-1.5, 0, 0], [1.5, 0, 0], [3, 0, 0], [0, 1.5, 0]]), boxes, 0
    )
    assert np.array_equal(labels, [0, -2, 1, -1])


def test_scan_selection_globally_excludes_holdout_neighbours_for_both_branches():
    scans = [
        {"index": 0, "timestamp": 0, "heldout": False, "is_key_frame": True},
        {"index": 1, "timestamp": 50000, "heldout": True, "is_key_frame": False},
        {"index": 2, "timestamp": 100000, "heldout": True, "is_key_frame": True},
        {"index": 3, "timestamp": 150000, "heldout": True, "is_key_frame": False},
        {"index": 4, "timestamp": 200000, "heldout": False, "is_key_frame": False},
        {"index": 5, "timestamp": 300000, "heldout": False, "is_key_frame": True},
    ]
    for timestamp in (0, 75000, 100000, 250000):
        assert set(
            allowed_scans({"scans": scans}, {"timestamp": timestamp}, "key")
        ) == {0, 5}
        assert set(
            allowed_scans({"scans": scans}, {"timestamp": timestamp}, "all")
        ) == {0, 4, 5}


def dynamic_fixture(category):
    r = Reconstruction.__new__(Reconstruction)
    first = box(time=0, translation=(0, 0, 10))
    last = box(time=100000, translation=(1, 0, 10))
    first["category"] = last["category"] = category
    r.tracks = ActorTracks([[first, last]])
    r.h, r.w = 352, 640
    r.views = [
        {"timestamp": 0, "camera": "CAM_FRONT"},
        {"timestamp": 100000, "camera": "CAM_FRONT"},
    ]
    r.by_camera = {
        c: [0, 1]
        for c in (
            "CAM_FRONT",
            "CAM_FRONT_RIGHT",
            "CAM_BACK_RIGHT",
            "CAM_BACK",
            "CAM_BACK_LEFT",
            "CAM_FRONT_LEFT",
        )
    }
    r.ks = np.repeat(np.array([[[100.0, 0, 320], [0, 100, 176], [0, 0, 1]]]), 2, axis=0)
    r.poses = np.repeat(np.eye(4)[None], 2, axis=0)
    r.dynamic_reference = np.full((2, 352, 640), 10.0, np.float32)
    r.actors = []
    for i in range(2):
        r.actors.append(
            {
                "xyz": np.array([[float(i), 0.0, 10.0]], np.float32),
                "rgb": np.array([[200, 100, 50]], np.uint8),
                "source": np.array([i], np.int16),
                "pixel": np.array([176 * 640 + 320 + i * 10], np.int32),
                "kind": np.array([2], np.uint8),
                "instance": np.array([0], np.int16),
            }
        )
    return r


def test_common_timestamp_compensation_uses_same_sources_and_removes_rigid_offset():
    r = dynamic_fixture("vehicle.car")
    clouds, recipe = r.dynamic_at(50000)
    assert recipe["pre_source_ids"] == 2
    assert len(clouds["unaligned"]["xyz"]) == 2
    assert len(clouds["compensated"]["xyz"]) == 1
    assert np.allclose(clouds["compensated"]["xyz"], [[0.5, 0, 10]])
    assert np.allclose(clouds["single"]["xyz"], [[0.5, 0, 10]])
    assert np.array_equal(clouds["single"]["rgb"], [[200, 100, 50]])


def test_nonrigid_and_articulated_actors_never_accumulate_history():
    for category in ("human.pedestrian.adult", "vehicle.bus.bendy", "vehicle.bicycle"):
        r = dynamic_fixture(category)
        clouds, recipe = r.dynamic_at(50000)
        assert recipe["pre_source_ids"] == 1
        assert all(len(clouds[mode]["xyz"]) == 1 for mode in clouds)
        assert all(
            np.array_equal(clouds[mode]["xyz"], clouds["single"]["xyz"])
            for mode in clouds
        )


def test_rear_camera_actor_is_not_removed_by_negative_front_camera_depth():
    r = dynamic_fixture("vehicle.car")
    r.views.append({"timestamp": 50000, "camera": "CAM_FRONT"})
    r.ks = np.concatenate([r.ks, r.ks[:1]])
    front_pose = np.eye(4)
    front_pose[:3, :3] = np.diag([-1, 1, -1])
    r.poses = np.concatenate([r.poses, front_pose[None]])
    r.dynamic_reference = np.concatenate([r.dynamic_reference, r.dynamic_reference[:1]])
    r.actors.append({key: value[:0].copy() for key, value in r.actors[0].items()})
    r.by_camera = {camera: [2] for camera in r.by_camera}
    r.by_camera["CAM_BACK"] = [0, 1]
    clouds, recipe = r.dynamic_at(50000)
    assert 0 in recipe["occlusion_reference_views"]
    assert len(clouds["compensated"]["xyz"]) == 1
    assert len(clouds["unaligned"]["xyz"]) == 2
