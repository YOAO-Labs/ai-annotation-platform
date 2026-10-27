"""v0.15.0 · Scene frame pose service

逐帧 ego pose 的存取:
- upsert_frame_poses:按 (scene_id, frame_index) upsert,幂等可重跑
  (importer / backfill 脚本共用)。
- get_trajectory:按 frame_index 升序取 scene 全轨迹。

本层只存取原始 ego→global 位姿;相对位移 / 插值是消费方(v0.15.1
ego_transform)的事,不在这里算。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.scene_pose import SceneFramePose
from app.schemas.scene_pose import FramePose


async def upsert_frame_poses(
    db: AsyncSession,
    *,
    scene_id: uuid.UUID,
    poses: list[FramePose],
) -> int:
    """批量 upsert 某 scene 的逐帧位姿,返回写入行数。

    冲突键 uq_scene_frame_pose(scene_id, frame_index) → DO UPDATE,
    重跑 importer / backfill 不产生重复行。
    """
    if not poses:
        return 0

    stmt = pg_insert(SceneFramePose).values(
        [
            {
                "id": uuid.uuid4(),
                "scene_id": scene_id,
                "frame_index": p.frame_index,
                "timestamp_us": p.timestamp_us,
                "ego_translation": p.ego_translation,
                "ego_rotation": p.ego_rotation,
                "source_metadata": p.source_metadata,
            }
            for p in poses
        ]
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_scene_frame_pose",
        set_={
            "timestamp_us": stmt.excluded.timestamp_us,
            "ego_translation": stmt.excluded.ego_translation,
            "ego_rotation": stmt.excluded.ego_rotation,
            "source_metadata": stmt.excluded.source_metadata,
        },
    )
    await db.execute(stmt)
    await db.flush()
    return len(poses)


async def get_trajectory(db: AsyncSession, scene_id: uuid.UUID) -> list[FramePose]:
    """scene 的有序逐帧轨迹(frame_index 升序);无位姿 → []。"""
    rows = await db.execute(
        select(SceneFramePose)
        .where(SceneFramePose.scene_id == scene_id)
        .order_by(SceneFramePose.frame_index)
    )
    return [FramePose.model_validate(row) for row in rows.scalars().all()]


async def get_frame_pose(
    db: AsyncSession, *, scene_id: uuid.UUID, frame_index: int
) -> FramePose | None:
    """单帧位姿(manifest 透出用);无行 → None。"""
    row = await db.scalar(
        select(SceneFramePose)
        .where(SceneFramePose.scene_id == scene_id)
        .where(SceneFramePose.frame_index == frame_index)
    )
    return FramePose.model_validate(row) if row is not None else None
