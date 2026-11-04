"""Portable role-path and multimodal frame grouping helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
import uuid


_ROLE_BOUNDARY_CHARS = ("_", "-", ".")
_POINT_CLOUD_EXTS = {".pcd", ".bin", ".ply", ".las", ".laz", ".npy"}
_DEFAULT_CAMERA_CHANNEL = "default"


@dataclass(frozen=True, slots=True)
class RolePatterns:
    lidar: tuple[str, ...] = ("lidar", "lidar_point_cloud", "velodyne", "points")
    camera: tuple[str, ...] = ("camera", "camera_image", "image", "cam")
    calib: tuple[str, ...] = ("calib", "calibration")


DEFAULT_ROLE_PATTERNS = RolePatterns()


@dataclass(frozen=True, slots=True)
class PreparedDatasetItem:
    """Minimal detached media facts used by grouping and scene planning."""

    id: uuid.UUID
    file_name: str
    file_path: str
    file_type: str
    scene_id: uuid.UUID | None = None
    frame_index: int | None = None
    logical_path: str | None = None


@dataclass(frozen=True, slots=True)
class GroupedFrame:
    lidar: PreparedDatasetItem | None
    cameras: tuple[tuple[str, PreparedDatasetItem], ...]


@dataclass(frozen=True, slots=True)
class PreparedPointCloudFrame:
    scene_id: uuid.UUID | None
    frame_id: str
    lidar: PreparedDatasetItem
    cameras: tuple[tuple[str, PreparedDatasetItem], ...]


def matches_role_part(part: str, patterns: tuple[str, ...]) -> bool:
    """Match aliases and separator suffixes such as ``camera_image_0``."""
    candidate = part.lower()
    for pattern in patterns:
        normalized = pattern.lower()
        if candidate == normalized or any(
            candidate.startswith(f"{normalized}{separator}")
            for separator in _ROLE_BOUNDARY_CHARS
        ):
            return True
    return False


def last_role_index(parts: tuple[str, ...], patterns: tuple[str, ...]) -> int:
    for index in range(len(parts) - 1, -1, -1):
        if matches_role_part(parts[index], patterns):
            return index
    return -1


def role_dir_names(
    patterns: RolePatterns = DEFAULT_ROLE_PATTERNS,
    *,
    extra: tuple[str, ...] = (),
) -> set[str]:
    return set(patterns.lidar + patterns.camera + patterns.calib + extra)


def matches_any_role_dir(
    part: str,
    patterns: RolePatterns = DEFAULT_ROLE_PATTERNS,
    *,
    extra: tuple[str, ...] = (),
) -> bool:
    return any(
        matches_role_part(part, candidates)
        for candidates in (patterns.lidar, patterns.camera, patterns.calib, extra)
    )


def is_point_cloud_item(item: PreparedDatasetItem) -> bool:
    return (
        item.file_type == "point_cloud"
        or PurePosixPath(logical_asset_path(item)).suffix.lower() in _POINT_CLOUD_EXTS
    )


def logical_asset_path(item: PreparedDatasetItem) -> str:
    """Return caller-provided logical paths or strip a valid publication prefix."""
    if item.logical_path is not None:
        return item.logical_path
    parts = PurePosixPath(item.file_path).parts
    if (
        len(parts) >= 5
        and parts[0] == "dataset-sources"
        and all(_is_uuid_part(part) for part in parts[1:4])
    ):
        return "/".join(parts[4:])
    return item.file_path


def source_ingest_logical_path(metadata: object) -> str | None:
    """Read a valid native source-ingest logical path from detached metadata."""
    if not isinstance(metadata, Mapping):
        return None
    marker = metadata.get("source_ingest")
    if not isinstance(marker, Mapping):
        return None
    operation_id = marker.get("operation_id")
    relpath = marker.get("relpath")
    if not isinstance(operation_id, str) or not isinstance(relpath, str) or not relpath:
        return None
    try:
        uuid.UUID(operation_id)
    except ValueError:
        return None
    if (
        relpath.startswith("/")
        or "\\" in relpath
        or any(part in {"", ".", ".."} for part in relpath.split("/"))
    ):
        return None
    return PurePosixPath(relpath).as_posix()


def _is_uuid_part(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


def group_frames(
    items: tuple[PreparedDatasetItem, ...] | list[PreparedDatasetItem],
    patterns: RolePatterns = DEFAULT_ROLE_PATTERNS,
) -> tuple[dict[str, GroupedFrame], dict[str, PreparedDatasetItem]]:
    """Group detached items by frame stem and camera calibration role."""
    frames: dict[str, dict] = {}
    calib_items: dict[str, PreparedDatasetItem] = {}
    camera_role_names = {pattern.lower() for pattern in patterns.camera}

    for item in items:
        path = PurePosixPath(logical_asset_path(item))
        parts = path.parts
        stem = path.stem
        suffix = path.suffix.lower()

        calib_index = last_role_index(parts, patterns.calib)
        if suffix == ".json" and (
            (
                len(parts) >= 3
                and matches_role_part(parts[-2], patterns.camera)
                and calib_index == len(parts) - 3
            )
            or calib_index == len(parts) - 2
        ):
            calib_items[stem] = item
            continue

        lidar_index = last_role_index(parts, patterns.lidar)
        if lidar_index >= 0 and is_point_cloud_item(item):
            frame = frames.setdefault(stem, {"lidar": None, "cameras": {}})
            frame["lidar"] = item
            continue

        camera_index = last_role_index(parts, patterns.camera)
        if camera_index >= 0 and camera_index + 1 < len(parts):
            role_dir = parts[camera_index]
            if camera_index + 1 < len(parts) - 1:
                camera = parts[camera_index + 1]
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
