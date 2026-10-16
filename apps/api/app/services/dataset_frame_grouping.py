from __future__ import annotations

from pathlib import PurePosixPath

from app.services.dataset_task_facts import GroupedFrame, PreparedDatasetItem
from app.services.role_patterns import (
    DEFAULT_ROLE_PATTERNS,
    RolePatterns,
    last_role_index,
    matches_role_part,
)

# point_cloud can be guessed as "other" during upload, so use the suffix as a fallback.
_POINT_CLOUD_EXTS = {".pcd", ".bin", ".ply", ".las", ".laz", ".npy"}
_DEFAULT_CAMERA_CHANNEL = "default"


def group_frames(
    items: tuple[PreparedDatasetItem, ...] | list[PreparedDatasetItem],
    patterns: RolePatterns = DEFAULT_ROLE_PATTERNS,
) -> tuple[dict[str, GroupedFrame], dict[str, PreparedDatasetItem]]:
    """Group detached dataset-item facts by frame and calibration camera.

    Role matching uses the last matching path segment. Calibration is detected
    before camera files so ``calib/camera/<channel>.json`` is not grouped as a
    frame image.
    """
    frames: dict[str, dict] = {}
    calib_items: dict[str, PreparedDatasetItem] = {}
    camera_role_names = {pattern.lower() for pattern in patterns.camera}

    for item in items:
        path = PurePosixPath(item.file_path)
        parts = path.parts
        stem = path.stem
        suffix = path.suffix.lower()

        calib_i = last_role_index(parts, patterns.calib)
        if suffix == ".json" and (
            (
                len(parts) >= 3
                and matches_role_part(parts[-2], patterns.camera)
                and calib_i == len(parts) - 3
            )
            or calib_i == len(parts) - 2
        ):
            calib_items[stem] = item
            continue

        lidar_i = last_role_index(parts, patterns.lidar)
        if lidar_i >= 0 and (
            item.file_type == "point_cloud" or suffix in _POINT_CLOUD_EXTS
        ):
            frame = frames.setdefault(stem, {"lidar": None, "cameras": {}})
            frame["lidar"] = item
            continue

        camera_i = last_role_index(parts, patterns.camera)
        if camera_i >= 0 and camera_i + 1 < len(parts):
            role_dir = parts[camera_i]
            if camera_i + 1 < len(parts) - 1:
                camera = parts[camera_i + 1]
            elif role_dir.lower() in camera_role_names:
                camera = _DEFAULT_CAMERA_CHANNEL
            else:
                camera = role_dir
            frame = frames.setdefault(stem, {"lidar": None, "cameras": {}})
            frame["cameras"][camera] = item

    grouped = {
        frame_id: GroupedFrame(
            lidar=frame["lidar"],
            cameras=tuple(frame["cameras"].items()),
        )
        for frame_id, frame in frames.items()
    }
    return grouped, calib_items
