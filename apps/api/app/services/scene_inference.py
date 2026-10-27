"""v0.14.0 · Scene inference

给已存在的 dataset 自动推断 scene 结构 + 给 items 赋 frame_index。

三种 mode:
- "single":全 dataset = 1 个 scene。scene.name 默认取 dataset.name。
- "per_subdirectory":按 file_path 中 dataset_name 之后第一段分组,每组一个 scene。
- "auto":顶层目录全是已知角色名 → single;否则 per_subdirectory。

幂等:
- 该 dataset 已有 scene → 整个跳过,不报错。
- 部分 items 已有 scene_id → 不动它们,只对 NULL items 跑;notes 警告。

frame_index 赋值算法(point_cloud / 多模态):
1. 调 group_frames 拿到 {frame_stem: {lidar, cameras}} + {cam: calib}。
2. frame_stem 自然排序("000001" < "000010")后 lidar 取 0..N-1。
3. 同帧 cam item 共享 lidar 的 frame_index。
4. calib_items 仅写 scene_id,frame_index=NULL。

非点云 dataset:按 file_name 自然排序赋 0..N-1,所有 items 都有 frame_index。
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import PurePosixPath
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import Dataset, DatasetItem
from app.schemas.scene import InferenceResult
from app.services import scene as scene_svc
from app.services.pointcloud_import import group_frames
from app.services.dataset_storage_keys import dataset_item_relative_path
from app.services.role_patterns import (
    DEFAULT_ROLE_PATTERNS,
    matches_any_role_dir,
    role_dir_names,
)

logger = logging.getLogger(__name__)

# 与 pointcloud_import.group_frames 共用 role pattern:这些顶层目录名说明
# "整 dataset 就是单 scene 的角色子目录布局";否则就是 per_subdirectory 的 scene key。
_EXTRA_ROLE_DIR_PATTERNS = ("video", "images", "videos")
ROLE_DIR_NAMES = role_dir_names(DEFAULT_ROLE_PATTERNS, extra=_EXTRA_ROLE_DIR_PATTERNS)

# 与 pointcloud_import 共享的扩展名集合
_POINT_CLOUD_EXTS = {".pcd", ".bin", ".ply", ".las", ".laz", ".npy"}

_SINGLE_GROUP_KEY = "_single"
_ROOT_GROUP_KEY = "_root"

# per_subdirectory 安全阀:推出超过这个数 → 报错,避免把巨型 jpg 序列误识别成 N 万个伪 scene
_MAX_INFERRED_SCENES = 100


SceneInferenceMode = Literal["single", "per_subdirectory", "auto"]


def _is_role_dir_name(part: str) -> bool:
    return matches_any_role_dir(
        part,
        DEFAULT_ROLE_PATTERNS,
        extra=_EXTRA_ROLE_DIR_PATTERNS,
    )


def _natural_sort_key(s: str) -> list:
    """自然排序 key('000001' < '000010' < 'a')。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _split_into_scene_groups(
    items: list[DatasetItem], dataset_name: str
) -> dict[str, list[DatasetItem]]:
    """把 items 按逻辑相对路径的第一段分组。

    跳过开头 dataset_name 段;若第一段是已知角色名 → 单 scene("_single");
    否则该段就是 scene 名(典型:nuScenes scene_token / SUSTech 多 scene zip 顶层目录)。
    无子目录的 item 落入 "_root" 组(理论不应出现于点云/帧序列 dataset)。
    """
    by_scene: dict[str, list[DatasetItem]] = {}
    for item in items:
        rest = PurePosixPath(dataset_item_relative_path(item, dataset_name)).parts
        if len(rest) < 2:
            by_scene.setdefault(_ROOT_GROUP_KEY, []).append(item)
            continue
        first = rest[0]
        key = _SINGLE_GROUP_KEY if _is_role_dir_name(first) else first
        by_scene.setdefault(key, []).append(item)
    return by_scene


def _is_pointcloud_like(items: list[DatasetItem]) -> bool:
    """是否含点云 / 多模态布局(有 lidar/ camera/ calib 角色目录)。"""
    for item in items:
        parts = PurePosixPath(dataset_item_relative_path(item)).parts
        if any(_is_role_dir_name(p) for p in parts):
            return True
        if (
            PurePosixPath(dataset_item_relative_path(item)).suffix.lower()
            in _POINT_CLOUD_EXTS
        ):
            return True
    return False


def _assign_frame_indices_pointcloud(
    scene_id: uuid.UUID, items: list[DatasetItem]
) -> tuple[int, list[str]]:
    """点云/多模态布局赋值:用 group_frames + 自然排序。

    返回 (assigned_count, notes)。
    """
    notes: list[str] = []
    frames, calib_items = group_frames(items)

    # 已分组的 item id 集合,用于计算"未参与分组"的 items(纯 metadata 等)
    grouped_ids: set[uuid.UUID] = set()

    sorted_stems = sorted(frames.keys(), key=_natural_sort_key)
    assigned = 0
    lidar_missing_stems: list[str] = []

    for fi, stem in enumerate(sorted_stems):
        frame = frames[stem]
        lidar = frame.get("lidar")
        cams = frame.get("cameras") or {}
        if lidar is None:
            lidar_missing_stems.append(stem)
        else:
            lidar.scene_id = scene_id
            lidar.frame_index = fi
            grouped_ids.add(lidar.id)
            assigned += 1
        for cam_item in cams.values():
            cam_item.scene_id = scene_id
            cam_item.frame_index = fi
            grouped_ids.add(cam_item.id)
            assigned += 1

    for cam_item in calib_items.values():
        cam_item.scene_id = scene_id
        cam_item.frame_index = None
        grouped_ids.add(cam_item.id)
        assigned += 1

    if lidar_missing_stems:
        notes.append(
            f"{len(lidar_missing_stems)} frames without lidar item, frame_index skipped"
        )
    if calib_items:
        notes.append(f"{len(calib_items)} calib items left frame_index=NULL")
    leftover = [it for it in items if it.id not in grouped_ids]
    if leftover:
        notes.append(
            f"{len(leftover)} items not matched by group_frames, left untouched"
        )

    return assigned, notes


def _assign_frame_indices_flat(
    scene_id: uuid.UUID, items: list[DatasetItem]
) -> tuple[int, list[str]]:
    """非点云 dataset(纯 image / video 帧序列):按 file_name 自然排序赋 0..N-1。"""
    sorted_items = sorted(items, key=lambda it: _natural_sort_key(it.file_name))
    for fi, item in enumerate(sorted_items):
        item.scene_id = scene_id
        item.frame_index = fi
    return len(sorted_items), []


async def infer_and_apply(
    db: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    mode: SceneInferenceMode = "auto",
    dry_run: bool = False,
) -> InferenceResult:
    """对 dataset 做 scene 推断 + frame_index 赋值。

    幂等:dataset 已有 scene → 跳过整个 inference,返回 created_scenes=0。
    部分 items 已有 scene_id → 跳过 inference,notes 报"部分迁移状态,人工检查"。
    """
    dataset = await db.get(Dataset, dataset_id)
    if dataset is None:
        return InferenceResult(
            dataset_id=dataset_id,
            created_scenes=0,
            assigned_items=0,
            skipped_items=0,
            dry_run=dry_run,
            notes=[f"dataset {dataset_id} not found"],
        )

    existing_scenes = await scene_svc.list_for_dataset(db, dataset_id)
    if existing_scenes:
        return InferenceResult(
            dataset_id=dataset_id,
            created_scenes=0,
            assigned_items=0,
            skipped_items=0,
            dry_run=dry_run,
            notes=[
                f"dataset already has {len(existing_scenes)} scene(s), inference skipped (idempotent)"
            ],
        )

    items_rows = (
        (
            await db.execute(
                select(DatasetItem).where(DatasetItem.dataset_id == dataset_id)
            )
        )
        .scalars()
        .all()
    )
    all_items: list[DatasetItem] = list(items_rows)
    if not all_items:
        return InferenceResult(
            dataset_id=dataset_id,
            created_scenes=0,
            assigned_items=0,
            skipped_items=0,
            dry_run=dry_run,
            notes=["dataset has no items"],
        )

    assigned_ids = [it.id for it in all_items if it.scene_id is not None]
    if assigned_ids:
        return InferenceResult(
            dataset_id=dataset_id,
            created_scenes=0,
            assigned_items=0,
            skipped_items=len(assigned_ids),
            dry_run=dry_run,
            notes=[
                f"{len(assigned_ids)} items already have scene_id; partial migration state, manual review required"
            ],
        )

    # 决定 scene 分组
    groups: dict[str, list[DatasetItem]]
    if mode == "single":
        groups = {_SINGLE_GROUP_KEY: all_items}
    else:
        groups = _split_into_scene_groups(all_items, dataset.name)
        if mode == "auto":
            keys = set(groups.keys())
            if keys.issubset({_SINGLE_GROUP_KEY, _ROOT_GROUP_KEY}):
                # 全是 role 目录布局 → 单 scene
                groups = {_SINGLE_GROUP_KEY: all_items}

    # 把 _root 抛出 notes,不参与建 scene
    notes: list[str] = []
    root_items = groups.pop(_ROOT_GROUP_KEY, None)
    if root_items:
        notes.append(
            f"{len(root_items)} items at dataset root (no subdirectory) skipped"
        )

    if not groups:
        return InferenceResult(
            dataset_id=dataset_id,
            created_scenes=0,
            assigned_items=0,
            skipped_items=len(all_items),
            dry_run=dry_run,
            notes=notes or ["no scene groups inferred"],
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

    created = 0
    assigned = 0
    for group_key, group_items in groups.items():
        scene_name = dataset.name if group_key == _SINGLE_GROUP_KEY else group_key
        if dry_run:
            created += 1
            assigned += len(group_items)
            notes.append(
                f"[dry-run] would create scene {scene_name!r} with {len(group_items)} items"
            )
            continue

        scene = await scene_svc.create_scene(
            db,
            dataset_id=dataset_id,
            name=scene_name,
            source_format="inferred",
            source_metadata={"mode": mode, "group_key": group_key},
        )
        if _is_pointcloud_like(group_items):
            count, sub_notes = _assign_frame_indices_pointcloud(scene.id, group_items)
        else:
            count, sub_notes = _assign_frame_indices_flat(scene.id, group_items)
        assigned += count
        notes.extend(f"[{scene_name}] {n}" for n in sub_notes)
        created += 1

    if not dry_run:
        await db.flush()

    return InferenceResult(
        dataset_id=dataset_id,
        created_scenes=created,
        assigned_items=assigned,
        skipped_items=0,
        dry_run=dry_run,
        notes=notes,
    )
