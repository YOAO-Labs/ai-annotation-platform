from __future__ import annotations

import uuid

from aap_assets_source.frame_layout import (
    DEFAULT_ROLE_PATTERNS,
    PreparedDatasetItem,
    RolePatterns,
    group_frames,
    logical_asset_path,
    matches_role_part,
    source_ingest_logical_path,
)


def _item(path: str, *, logical_path: str | None = None, file_type="image"):
    return PreparedDatasetItem(
        id=uuid.uuid4(),
        file_name=path.rsplit("/", 1)[-1],
        file_path=path,
        logical_path=logical_path,
        file_type=file_type,
    )


def test_role_aliases_match_case_insensitively_and_on_separator_suffixes():
    assert matches_role_part("LiDaR_Point_Cloud_0", DEFAULT_ROLE_PATTERNS.lidar)
    assert matches_role_part("CAMERA-image-0", DEFAULT_ROLE_PATTERNS.camera)
    assert not matches_role_part("lidarish", DEFAULT_ROLE_PATTERNS.lidar)
    assert matches_role_part("lidar-custom", ("lidar",))


def test_group_frames_uses_legacy_and_explicit_logical_paths():
    lidar = _item("dataset/lidar/000010.pcd", file_type="point_cloud")
    camera = _item(
        "dataset-sources/00000000-0000-0000-0000-000000000001/"
        "00000000-0000-0000-0000-000000000002/"
        "00000000-0000-0000-0000-000000000003/camera/front/000010.jpg",
        file_type="image",
    )
    calib = _item(
        "physical/object/key/calib/camera/front.json",
        logical_path="dataset/calib/camera/front.json",
        file_type="other",
    )

    assert logical_asset_path(camera) == "camera/front/000010.jpg"
    assert logical_asset_path(calib) == "dataset/calib/camera/front.json"
    frames, calibrations = group_frames([lidar, camera, calib])
    assert set(frames) == {"000010"}
    assert frames["000010"].lidar == lidar
    assert frames["000010"].cameras == (("front", camera),)
    assert calibrations == {"front": calib}


def test_group_frames_supports_role_aliases_and_flat_default_channel():
    lidar = _item("lidar_point_cloud_0/frame_1.pcd", file_type="other")
    camera = _item("camera_image_0/frame_1.jpg")
    flat_camera = _item("camera/frame_2.jpg")
    frames, _ = group_frames([lidar, camera, flat_camera])
    assert frames["frame_1"].lidar == lidar
    assert frames["frame_1"].cameras == (("camera_image_0", camera),)
    assert frames["frame_2"].cameras == (("default", flat_camera),)


def test_group_frames_respects_custom_role_patterns():
    lidar = _item("scanner/frame.pcd", file_type="point_cloud")
    camera = _item("imager/front/frame.jpg")
    frames, _ = group_frames(
        [lidar, camera],
        RolePatterns(lidar=("scanner",), camera=("imager",), calib=("cal",)),
    )
    assert frames["frame"].lidar == lidar
    assert frames["frame"].cameras == (("front", camera),)


def test_native_source_ingest_marker_supplies_only_valid_relative_logical_paths():
    operation_id = str(uuid.uuid4())
    assert (
        source_ingest_logical_path(
            {
                "source_ingest": {
                    "operation_id": operation_id,
                    "relpath": "scene_a/lidar/1.pcd",
                }
            }
        )
        == "scene_a/lidar/1.pcd"
    )
    for relpath in (
        "../escape.jpg",
        "/absolute.jpg",
        "scene//image.jpg",
        "scene\\image.jpg",
    ):
        assert (
            source_ingest_logical_path(
                {"source_ingest": {"operation_id": operation_id, "relpath": relpath}}
            )
            is None
        )
