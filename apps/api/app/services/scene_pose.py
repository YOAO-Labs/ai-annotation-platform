"""Scene frame pose service

逐帧 ego pose 的存取:
- upsert_frame_poses:按 (scene_id, frame_index) upsert,幂等可重跑
  (importer / backfill 脚本共用)。
- get_trajectory:按 frame_index 升序取 scene 全轨迹。

本层只存取原始 ego→global 位姿；相对位移与插值由消费方
（如 ego_transform）计算。
"""

from __future__ import annotations

import uuid

from yoao_assets_catalog.spatial_contract import (
    ReadPosesRequest,
    UpsertPosesRequest,
    WireFramePose,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.assets_spatial import (
    AssetsSpatialGatewayError,
    SameSessionAssetsSpatialGateway,
)
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

    result = await SameSessionAssetsSpatialGateway(db).upsert_poses(
        UpsertPosesRequest(
            operation_id=uuid.uuid4(),
            scene_id=scene_id,
            poses=[WireFramePose.model_validate(pose) for pose in poses],
        )
    )
    return result.upserted


async def get_trajectory(db: AsyncSession, scene_id: uuid.UUID) -> list[FramePose]:
    """scene 的有序逐帧轨迹(frame_index 升序);无位姿 → []。"""
    try:
        return await SameSessionAssetsSpatialGateway(db).read_poses(
            ReadPosesRequest(actor_id=None, scene_id=scene_id)
        )
    except AssetsSpatialGatewayError as exc:
        if exc.code != "scene_missing":
            raise
        return []


async def get_frame_pose(
    db: AsyncSession, *, scene_id: uuid.UUID, frame_index: int
) -> FramePose | None:
    """单帧位姿(manifest 透出用);无行 → None。"""
    if frame_index < 0:
        return None
    pair = (scene_id, frame_index)
    return (await SameSessionAssetsSpatialGateway(db).read_pose_pairs({pair})).get(pair)
