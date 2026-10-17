"""v0.14.0 · Scenes API

跨 task 帧序列地基的 CRUD 端点。本期不挂"创建 scene"——scene 由两种方式产生:
1. 导入 hook 自动创建(pointcloud_import / upload-zip 末尾跑 scene_inference)
2. backfill 脚本对历史 dataset 一次性创建

故此 router 只暴露读和 PATCH;真正的 create 由 services 层做。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.deps import get_current_user, get_db
from app.schemas.scene import SceneOut, SceneUpdate
from app.schemas.scene_pose import TrajectoryResponse
from app.services import scene as scene_svc
from app.services import scene_pose as scene_pose_svc

router = APIRouter()


@router.get("", response_model=list[SceneOut])
async def list_scenes(
    dataset_id: uuid.UUID = Query(..., description="按 dataset 过滤"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """列 dataset 下所有 scene(按 created_at 升序)。"""
    scenes = await scene_svc.list_for_dataset(db, dataset_id)
    return [SceneOut.model_validate(s) for s in scenes]


@router.get("/{scene_id}", response_model=SceneOut)
async def get_scene(
    scene_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    scene = await scene_svc.get_scene(db, scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    return SceneOut.model_validate(scene)


@router.get("/{scene_id}/trajectory", response_model=TrajectoryResponse)
async def get_scene_trajectory(
    scene_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """v0.15.0 · scene 的有序逐帧 ego 轨迹(frame_index 升序)。

    无位姿 scene(历史数据 / 非 nuScenes 来源)→ 200 + poses=[],
    消费方按"无轨迹"降级,不报错。
    """
    scene = await scene_svc.get_scene(db, scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    poses = await scene_pose_svc.get_trajectory(db, scene_id)
    return TrajectoryResponse(scene_id=scene_id, poses=poses)


@router.patch("/{scene_id}", response_model=SceneOut)
async def update_scene(
    scene_id: uuid.UUID,
    payload: SceneUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新 scene 元数据(name / source_format / source_metadata)。

    name 冲突 → 409。
    """
    try:
        scene = await scene_svc.update_scene_fields(
            db,
            scene_id=scene_id,
            name=payload.name,
            source_format=payload.source_format,
            source_metadata=payload.source_metadata,
        )
    except scene_svc.SceneNameConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if scene is None:
        raise HTTPException(status_code=404, detail="Scene not found")

    await db.commit()
    # refresh:刷新 server_default / onupdate 的 updated_at 等延迟字段,避免
    # Pydantic 在异步序列化时触发 MissingGreenlet。
    await db.refresh(scene)
    return SceneOut.model_validate(scene)
