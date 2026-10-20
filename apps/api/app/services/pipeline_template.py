"""v0.21.0 · 命名编排权限与应用辅助函数."""

from __future__ import annotations

import copy
import uuid

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.project_pipeline import ProjectPipeline
from app.db.models.user import User
from app.services.project_template import list_user_org_ids


async def assert_pipeline_visible(
    db: AsyncSession, pipeline: ProjectPipeline, user: User
) -> None:
    """看不到则 404 隐藏存在性."""
    if user.role == UserRole.SUPER_ADMIN:
        return
    if pipeline.scope == "public":
        return
    if pipeline.scope == "private" and pipeline.created_by == user.id:
        return
    if pipeline.scope == "organization" and pipeline.organization_id is not None:
        org_ids = await list_user_org_ids(db, user)
        if pipeline.organization_id in org_ids:
            return
    raise HTTPException(status_code=404, detail="编排不存在")


async def can_edit_pipeline(
    db: AsyncSession, pipeline: ProjectPipeline, user: User
) -> bool:
    if user.role == UserRole.SUPER_ADMIN:
        return True
    if pipeline.created_by == user.id:
        return True
    if pipeline.scope == "organization" and pipeline.organization_id is not None:
        org_ids = await list_user_org_ids(db, user)
        return pipeline.organization_id in org_ids
    return False


async def assert_can_create_pipeline_scope(
    db: AsyncSession, scope: str, organization_id: uuid.UUID | None, user: User
) -> None:
    if scope == "public" and user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="仅超级管理员可创建公共编排")
    if scope == "organization":
        if organization_id is None:
            raise HTTPException(
                status_code=400, detail="organization scope 必须指定 organization_id"
            )
        if user.role != UserRole.SUPER_ADMIN:
            org_ids = await list_user_org_ids(db, user)
            if organization_id not in org_ids:
                raise HTTPException(status_code=403, detail="无权创建该组织编排")


def copy_pipeline_stages(stages: list) -> list:
    return copy.deepcopy(stages)


async def switch_project_default_pipeline(
    db: AsyncSession, project_id: uuid.UUID, pipeline_id: uuid.UUID | None = None
) -> None:
    await db.execute(
        update(ProjectPipeline)
        .where(
            ProjectPipeline.project_id == project_id,
            ProjectPipeline.is_default.is_(True),
        )
        .values(is_default=False)
    )
    if pipeline_id is not None:
        await db.execute(
            update(ProjectPipeline)
            .where(ProjectPipeline.id == pipeline_id)
            .values(is_default=True)
        )


async def unenabled_backend_ids(
    db: AsyncSession, project_id: uuid.UUID, stages: list
) -> list[str]:
    from app.services.ml_backend import MLBackendService

    svc = MLBackendService(db)
    missing: list[str] = []
    seen: set[str] = set()
    for stage in stages:
        if not isinstance(stage, dict) or not stage.get("ml_backend_id"):
            continue
        backend_id = str(stage["ml_backend_id"])
        if backend_id in seen:
            continue
        seen.add(backend_id)
        try:
            parsed = uuid.UUID(backend_id)
        except ValueError:
            missing.append(backend_id)
            continue
        backend = await svc.get(parsed)
        if not backend or not await svc.is_enabled(project_id, parsed):
            missing.append(backend_id)
    return missing


async def assert_model_reference_scope(db, stages, scope, project_id):
    from app.services.trained_models import is_controlled, load_version

    for stage in stages:
        model_id = stage.get("model_id")
        if not is_controlled(model_id):
            continue
        if scope != "private" or project_id is None:
            raise HTTPException(
                status_code=422, detail="项目训练模型只能保存在所属项目的私有管线中"
            )
        if model_id.startswith("trained:"):
            try:
                version_id = uuid.UUID(model_id.removeprefix("trained:"))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="模型版本标识无效") from exc
            await load_version(db, project_id, version_id)
