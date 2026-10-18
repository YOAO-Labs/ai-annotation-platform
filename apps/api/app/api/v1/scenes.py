"""v0.14.0 · Scenes API

跨 task 帧序列地基的 CRUD 端点。本期不挂"创建 scene"——scene 由两种方式产生:
1. 导入 hook 自动创建(pointcloud_import / upload-zip 末尾跑 owner scene backfill)
2. backfill 脚本对历史 dataset 一次性创建

故此 router 只暴露读和 PATCH;真正的 create 由 services 层做。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_roles, require_scopes
from app.schemas.scene import SceneOut, SceneUpdate
from app.schemas.scene_pose import TrajectoryResponse
from aap_assets_catalog.spatial_contract import (
    GetSceneRequest,
    ListScenesRequest,
    MutateSceneRequest,
    ReadPosesRequest,
    ScenePatch,
)
from app.services.assets_spatial import AssetsSpatialGateway, AssetsSpatialGatewayError

router = APIRouter()
_MANAGERS = (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)


@router.get(
    "",
    response_model=list[SceneOut],
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def list_scenes(
    dataset_id: uuid.UUID = Query(..., description="按 dataset 过滤"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """列 dataset 下所有 scene(按 created_at 升序)。"""
    gateway = AssetsSpatialGateway(db)
    await gateway.guard_actor(current_user.id, "read")
    try:
        scenes = await gateway.list_scenes(
            ListScenesRequest(actor_id=current_user.id, dataset_id=dataset_id)
        )
    except AssetsSpatialGatewayError as exc:
        if exc.code == "dataset_missing":
            return []
        raise
    return [SceneOut.model_validate(s) for s in scenes]


@router.get(
    "/{scene_id}",
    response_model=SceneOut,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def get_scene(
    scene_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    gateway = AssetsSpatialGateway(db)
    await gateway.guard_actor(current_user.id, "read")
    try:
        result = await gateway.get_scene(
            GetSceneRequest(actor_id=current_user.id, scene_id=scene_id)
        )
    except AssetsSpatialGatewayError as exc:
        if exc.code == "scene_missing":
            raise HTTPException(status_code=404, detail="Scene not found") from exc
        raise
    return SceneOut.model_validate(result.scene)


@router.get(
    "/{scene_id}/trajectory",
    response_model=TrajectoryResponse,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def get_scene_trajectory(
    scene_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """v0.15.0 · scene 的有序逐帧 ego 轨迹(frame_index 升序)。

    无位姿 scene(历史数据 / 非 nuScenes 来源)→ 200 + poses=[],
    消费方按"无轨迹"降级,不报错。
    """
    gateway = AssetsSpatialGateway(db)
    await gateway.guard_actor(current_user.id, "read")
    try:
        await gateway.get_scene(
            GetSceneRequest(actor_id=current_user.id, scene_id=scene_id)
        )
        poses = await gateway.read_poses(
            ReadPosesRequest(actor_id=current_user.id, scene_id=scene_id)
        )
    except AssetsSpatialGatewayError as exc:
        if exc.code == "scene_missing":
            raise HTTPException(status_code=404, detail="Scene not found") from exc
        raise
    return TrajectoryResponse(scene_id=scene_id, poses=poses)


@router.patch(
    "/{scene_id}",
    response_model=SceneOut,
    responses={
        200: {
            "headers": {
                "Idempotency-Key": {
                    "description": "Reuse this UUID to replay the committed scene update.",
                    "schema": {"type": "string", "format": "uuid"},
                }
            }
        }
    },
)
async def update_scene(
    scene_id: uuid.UUID,
    payload: SceneUpdate,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    """更新 scene 元数据(name / source_format / source_metadata)。

    name 冲突 → 409。
    """
    operation_id = idempotency_key or uuid.uuid4()
    gateway = AssetsSpatialGateway(db)
    try:
        result = await gateway.mutate_scene(
            MutateSceneRequest(
                operation_id=operation_id,
                actor_id=current_user.id,
                scene_id=scene_id,
                patch=ScenePatch.model_validate(
                    payload.model_dump(exclude_unset=True), strict=True
                ),
                audit_context={"path": f"/scenes/{scene_id}", "method": "PATCH"},
            )
        )
    except AssetsSpatialGatewayError as exc:
        if exc.code == "scene_missing":
            raise HTTPException(status_code=404, detail="Scene not found") from exc
        if exc.code == "scene_conflict":
            raise HTTPException(status_code=409, detail=exc.message) from exc
        raise
    response.headers["Idempotency-Key"] = str(operation_id)
    return SceneOut.model_validate(result.scene)
