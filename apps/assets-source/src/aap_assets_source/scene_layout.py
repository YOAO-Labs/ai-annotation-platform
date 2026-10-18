"""Pure scene grouping and frame-index planning for dataset media layouts."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from aap_assets_source.frame_layout import (
    DEFAULT_ROLE_PATTERNS,
    PreparedDatasetItem,
    group_frames,
    logical_asset_path,
    matches_any_role_dir,
    role_dir_names,
)

SceneInferenceMode = Literal["single", "per_subdirectory", "auto"]

_EXTRA_ROLE_DIR_PATTERNS = ("video", "images", "videos")
ROLE_DIR_NAMES = role_dir_names(DEFAULT_ROLE_PATTERNS, extra=_EXTRA_ROLE_DIR_PATTERNS)
_POINT_CLOUD_EXTS = {".pcd", ".bin", ".ply", ".las", ".laz", ".npy"}
_SINGLE_GROUP_KEY = "_single"
_ROOT_GROUP_KEY = "_root"
_MAX_INFERRED_SCENES = 100


@dataclass(frozen=True, slots=True)
class SceneLayoutInput:
    dataset_name: str
    items: tuple[PreparedDatasetItem, ...]
    scene_count: int = 0
    assigned_count: int | None = None
    item_count: int | None = None
    mode: SceneInferenceMode = "auto"
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class PlannedScene:
    id: uuid.UUID
    name: str
    source_format: str
    source_metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class ItemSceneAssignment:
    item_id: uuid.UUID
    scene_id: uuid.UUID
    frame_index: int | None


@dataclass(frozen=True, slots=True)
class IngestScenePlan:
    mode: SceneInferenceMode
    dry_run: bool
    scenes: tuple[PlannedScene, ...]
    assignments: tuple[ItemSceneAssignment, ...]
    created_scenes: int
    assigned_items: int
    skipped_items: int
    notes: tuple[str, ...]


def natural_sort_key(value: str) -> tuple[tuple[int, int | str], ...]:
    """Sort numeric frame stems in human order (for example 2 before 10)."""
    return tuple(
        (0, int(token)) if token.isdigit() else (1, token.lower())
        for token in re.split(r"(\d+)", value)
    )


def is_role_dir_name(part: str) -> bool:
    return matches_any_role_dir(
        part,
        DEFAULT_ROLE_PATTERNS,
        extra=_EXTRA_ROLE_DIR_PATTERNS,
    )


def _split_into_scene_groups(
    items: tuple[PreparedDatasetItem, ...], dataset_name: str
) -> dict[str, list[PreparedDatasetItem]]:
    by_scene: dict[str, list[PreparedDatasetItem]] = {}
    for item in items:
        parts = PurePosixPath(logical_asset_path(item)).parts
        start = 1 if parts and parts[0] == dataset_name else 0
        rest = parts[start:]
        if len(rest) < 2:
            by_scene.setdefault(_ROOT_GROUP_KEY, []).append(item)
            continue
        first = rest[0]
        key = _SINGLE_GROUP_KEY if is_role_dir_name(first) else first
        by_scene.setdefault(key, []).append(item)
    return by_scene


def _is_pointcloud_like(items: tuple[PreparedDatasetItem, ...]) -> bool:
    for item in items:
        logical_path = logical_asset_path(item)
        parts = PurePosixPath(logical_path).parts
        if any(is_role_dir_name(part) for part in parts):
            return True
        if PurePosixPath(logical_path).suffix.lower() in _POINT_CLOUD_EXTS:
            return True
    return False


def _pointcloud_assignments(
    scene_id: uuid.UUID, items: tuple[PreparedDatasetItem, ...]
) -> tuple[list[ItemSceneAssignment], list[str]]:
    notes: list[str] = []
    frames, calib_items = group_frames(items)
    assignments: list[ItemSceneAssignment] = []
    assigned_ids: set[uuid.UUID] = set()
    lidar_missing_stems: list[str] = []

    for frame_index, stem in enumerate(sorted(frames, key=natural_sort_key)):
        frame = frames[stem]
        if frame.lidar is None:
            lidar_missing_stems.append(stem)
        else:
            assignments.append(
                ItemSceneAssignment(frame.lidar.id, scene_id, frame_index)
            )
            assigned_ids.add(frame.lidar.id)
        for _camera, camera_item in frame.cameras:
            assignments.append(
                ItemSceneAssignment(camera_item.id, scene_id, frame_index)
            )
            assigned_ids.add(camera_item.id)

    for calib_item in calib_items.values():
        assignments.append(ItemSceneAssignment(calib_item.id, scene_id, None))
        assigned_ids.add(calib_item.id)

    if lidar_missing_stems:
        notes.append(
            f"{len(lidar_missing_stems)} frames without lidar item, frame_index skipped"
        )
    if calib_items:
        notes.append(f"{len(calib_items)} calib items left frame_index=NULL")
    leftover = [item for item in items if item.id not in assigned_ids]
    if leftover:
        notes.append(
            f"{len(leftover)} items not matched by group_frames, left untouched"
        )
    return assignments, notes


def _flat_assignments(
    scene_id: uuid.UUID, items: tuple[PreparedDatasetItem, ...]
) -> list[ItemSceneAssignment]:
    sorted_items = sorted(items, key=lambda item: natural_sort_key(item.file_name))
    return [
        ItemSceneAssignment(item.id, scene_id, frame_index)
        for frame_index, item in enumerate(sorted_items)
    ]


def plan_scenes(request: SceneLayoutInput) -> IngestScenePlan:
    """Plan Scene rows and item frame assignments without ORM or persistence."""
    items = request.items
    item_count = len(items) if request.item_count is None else request.item_count
    assigned_count = (
        sum(item.scene_id is not None for item in items)
        if request.assigned_count is None
        else request.assigned_count
    )
    if request.scene_count:
        return IngestScenePlan(
            request.mode,
            request.dry_run,
            (),
            (),
            0,
            0,
            0,
            (
                f"dataset already has {request.scene_count} scene(s), "
                "inference skipped (idempotent)",
            ),
        )
    if item_count == 0:
        return IngestScenePlan(
            request.mode,
            request.dry_run,
            (),
            (),
            0,
            0,
            0,
            ("dataset has no items",),
        )
    if assigned_count:
        return IngestScenePlan(
            request.mode,
            request.dry_run,
            (),
            (),
            0,
            0,
            assigned_count,
            (
                f"{assigned_count} items already have scene_id; "
                "partial migration state, manual review required",
            ),
        )

    if request.mode == "single":
        groups = {_SINGLE_GROUP_KEY: list(items)}
    else:
        groups = _split_into_scene_groups(items, request.dataset_name)
        if request.mode == "auto" and set(groups).issubset(
            {_SINGLE_GROUP_KEY, _ROOT_GROUP_KEY}
        ):
            groups = {_SINGLE_GROUP_KEY: list(items)}

    notes: list[str] = []
    root_items = groups.pop(_ROOT_GROUP_KEY, None)
    if root_items:
        notes.append(
            f"{len(root_items)} items at dataset root (no subdirectory) skipped"
        )
    if not groups:
        return IngestScenePlan(
            request.mode,
            request.dry_run,
            (),
            (),
            0,
            0,
            item_count,
            tuple(notes or ["no scene groups inferred"]),
        )
    if len(groups) > _MAX_INFERRED_SCENES:
        role_names = ", ".join(sorted(ROLE_DIR_NAMES))
        raise ValueError(
            f"inferred {len(groups)} scenes > {_MAX_INFERRED_SCENES}; "
            "likely misidentified a flat or pseudo multi-scene ZIP. "
            f"Top-level scene directories must not use reserved role names ({role_names}); "
            "for large multi-scene imports use the conversion script. "
            "See docs-site/user-guide/datasets/import-formats.md"
        )

    scenes: list[PlannedScene] = []
    assignments: list[ItemSceneAssignment] = []
    assigned_count = 0
    for group_key, group_rows in groups.items():
        group_items = tuple(group_rows)
        scene_name = (
            request.dataset_name if group_key == _SINGLE_GROUP_KEY else group_key
        )
        scene_id = uuid.uuid4()
        scene = PlannedScene(
            id=scene_id,
            name=scene_name,
            source_format="inferred",
            source_metadata={"mode": request.mode, "group_key": group_key},
        )
        scenes.append(scene)
        if _is_pointcloud_like(group_items):
            group_assignments, group_notes = _pointcloud_assignments(
                scene_id, group_items
            )
            assignments.extend(group_assignments)
            assigned = len(group_assignments)
            notes.extend(f"[{scene_name}] {note}" for note in group_notes)
        else:
            group_assignments = _flat_assignments(scene_id, group_items)
            assignments.extend(group_assignments)
            assigned = len(group_items)
        if request.dry_run:
            assigned_count += len(group_items)
            notes.append(
                f"[dry-run] would create scene {scene_name!r} "
                f"with {len(group_items)} items"
            )
        else:
            assigned_count += assigned

    return IngestScenePlan(
        request.mode,
        request.dry_run,
        tuple(scenes),
        tuple(assignments),
        len(scenes),
        assigned_count,
        0,
        tuple(notes),
    )
