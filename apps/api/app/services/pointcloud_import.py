"""Point-cloud dataset asset preparation and calibration."""

from __future__ import annotations

import json
import logging
import uuid

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_source.frame_layout import (
    PreparedDatasetItem,
    PreparedPointCloudFrame,
    group_frames,
    source_ingest_logical_path,
)
from app.db.models.dataset import DatasetItem
from aap_assets_catalog.spatial_values import SensorCalibration
from app.services.storage import storage_service

logger = logging.getLogger(__name__)


async def _load_dataset_items(
    db: AsyncSession, dataset_id: uuid.UUID
) -> list[DatasetItem]:
    result = await db.execute(
        select(DatasetItem).where(DatasetItem.dataset_id == dataset_id)
    )
    return list(result.scalars().all())


async def _maybe_infer_single_scene(db: AsyncSession, *, dataset_id: uuid.UUID) -> None:
    """v0.14.0 · 点云 link 接线前的 scene hook。

    若 dataset 已有 scene 直接返回;否则跑 single-mode inference。
    SUSTechPOINTS / wizard 上传走这里 → 自动建出 1 scene + frame_index。
    """
    from app.services import scene as scene_svc
    from app.services import scene_inference

    existing = await scene_svc.list_for_dataset(db, dataset_id)
    if existing:
        return
    await scene_inference.infer_and_apply(db, dataset_id=dataset_id, mode="single")
    await db.flush()


async def prepare_pointcloud_frames(
    db: AsyncSession, *, dataset_id: uuid.UUID
) -> tuple[PreparedPointCloudFrame, ...]:
    """Infer the single-scene layout when needed and return detached frame facts."""
    await _maybe_infer_single_scene(db, dataset_id=dataset_id)

    items = await _load_dataset_items(db, dataset_id)
    item_facts = tuple(
        PreparedDatasetItem(
            id=item.id,
            file_name=item.file_name,
            file_path=item.file_path,
            file_type=item.file_type,
            scene_id=item.scene_id,
            logical_path=getattr(item, "logical_path", None)
            or source_ingest_logical_path(item.metadata_),
        )
        for item in items
    )
    buckets: dict[uuid.UUID | None, list[PreparedDatasetItem]] = {}
    for item in item_facts:
        buckets.setdefault(item.scene_id, []).append(item)

    frames = {}
    for scene_id, scene_items in buckets.items():
        scene_frames, _ = group_frames(scene_items)
        for frame_id, frame in scene_frames.items():
            frames[(scene_id, frame_id)] = frame

    prepared: list[PreparedPointCloudFrame] = []
    for frame_key in sorted(frames, key=lambda key: (str(key[0]), key[1])):
        frame = frames[frame_key]
        if frame.lidar is None:
            logger.warning(
                "pointcloud frame %r has no lidar item, skipped (dataset=%s)",
                frame_key,
                dataset_id,
            )
            continue
        prepared.append(
            PreparedPointCloudFrame(
                scene_id=frame_key[0],
                frame_id=frame_key[1],
                lidar=frame.lidar,
                cameras=frame.cameras,
            )
        )
    return tuple(prepared)


async def attach_calibration(db: AsyncSession, *, dataset_id: uuid.UUID) -> int:
    """v0.13.1 · 读 calib/camera/<cam>.json 写入该相机所有帧 DatasetItem 的 metadata。

    对每个 calib item：从 MinIO 读字节 → json.loads → 经 SensorCalibration 归一化后落库。
    归一化做两件事（v0.13.2 起，原先存原始 dict + 仅长度校验）：
      1. 只保留 schema 已建模字段（extrinsic / intrinsic / rect），剥掉上游/厂商夹带的
         杂键（如 SUSTechPOINTS 示例 left.json 的 extrinsic_ok）；
      2. 过 SensorCalibration 校验（extrinsic=16 / intrinsic=9 / rect=16），非法则
         warning 跳过该相机（不抛）。
    入库即「单一真值的干净标定」，读取端（manifest）不会再因杂键被 extra="forbid" 判废。
    返回写入的相机项数（DatasetItem 行数）。
    """
    items = await _load_dataset_items(db, dataset_id)
    items_by_id = {item.id: item for item in items}
    item_facts = tuple(
        PreparedDatasetItem(
            id=item.id,
            file_name=item.file_name,
            file_path=item.file_path,
            file_type=item.file_type,
            scene_id=item.scene_id,
            logical_path=getattr(item, "logical_path", None)
            or source_ingest_logical_path(item.metadata_),
        )
        for item in items
    )
    frames, calib_items = group_frames(item_facts)

    cam_to_item_ids: dict[str, list[uuid.UUID]] = {}
    for frame in frames.values():
        for cam, cam_item in frame.cameras:
            cam_to_item_ids.setdefault(cam, []).append(cam_item.id)

    known_fields = set(SensorCalibration.model_fields)
    written = 0
    for cam, calib_item in calib_items.items():
        raw = _read_calibration(calib_item.file_path)
        if raw is None:
            continue
        filtered = {k: v for k, v in raw.items() if k in known_fields}
        try:
            calib = SensorCalibration.model_validate(filtered)
        except ValidationError as exc:
            logger.warning("calibration for camera %r invalid, skipped: %s", cam, exc)
            continue
        normalized = calib.model_dump(exclude_none=True)

        for cam_item_id in cam_to_item_ids.get(cam, []):
            cam_item = items_by_id[cam_item_id]
            cam_item.metadata_ = {
                **(cam_item.metadata_ or {}),
                "calibration": normalized,
            }
            written += 1

    await db.flush()
    return written


def _read_calibration(key: str) -> dict | None:
    """从 datasets 桶读取 calib JSON 字节并解析。读取 / 解析失败返回 None。

    抽成小函数便于测试注入（monkeypatch storage_service.client.get_object 即可）。
    """
    try:
        body = storage_service.client.get_object(
            Bucket=storage_service.datasets_bucket, Key=key
        )["Body"].read()
        return json.loads(body)
    except Exception:
        logger.warning("failed to read calibration object %r", key, exc_info=True)
        return None
