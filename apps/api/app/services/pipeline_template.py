"""v0.21.0 · 命名编排权限与应用辅助函数."""

from __future__ import annotations

import copy
import uuid

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.project_pipeline import ProjectPipeline
from app.schemas.preannotate import PreannotateRequest
from app.schemas.project_pipeline import (
    ProjectPipelineCreate,
    ProjectPipelineUpdate,
    _validate_scope_owner,
)
from app.services.project_access import resolve_project_access_by_id
from app.services.project_template import list_user_org_ids
from app.services.identity_contracts import IdentityActorFacts


def validate_saved_pipeline(stages) -> list[dict] | None:
    """Validate saved stages structurally, without backend availability checks.

    PipelineStage normalizes stored backend ID strings through the public request
    schema. Backend existence and project enablement are apply-time checks.
    """
    if not stages:
        return stages
    if not isinstance(stages, list) or not all(isinstance(s, dict) for s in stages):
        raise HTTPException(
            status_code=422, detail="preannotate_pipeline 须为阶段对象数组"
        )
    roots = [s for s in stages if s.get("parent_stage") is None]
    if len(roots) != 1:
        raise HTTPException(
            status_code=422,
            detail="preannotate_pipeline 须恰有一个源阶段 (parent_stage=None)",
        )
    try:
        parsed = PreannotateRequest(
            ml_backend_id=roots[0].get("ml_backend_id"), pipeline_stages=stages
        )
        return [
            stage.model_dump(mode="json", exclude_unset=True)
            for stage in parsed.pipeline_stages
        ]
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=f"preannotate_pipeline 校验失败: {exc}"
        ) from exc


async def _assert_project_visible(
    db: AsyncSession, project_id: uuid.UUID, user: IdentityActorFacts
) -> None:
    await resolve_project_access_by_id(db, actor_id=user.id, project_id=project_id)


async def assert_pipeline_visible(
    db: AsyncSession, pipeline: ProjectPipeline, user: IdentityActorFacts
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
    db: AsyncSession, pipeline: ProjectPipeline, user: IdentityActorFacts
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
    db: AsyncSession,
    scope: str,
    organization_id: uuid.UUID | None,
    user: IdentityActorFacts,
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


async def create_pipeline(
    db: AsyncSession, data: ProjectPipelineCreate, user: IdentityActorFacts
) -> ProjectPipeline:
    if user.role not in (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN):
        raise HTTPException(
            status_code=403, detail="仅项目管理员或超级管理员可创建编排"
        )
    await assert_can_create_pipeline_scope(db, data.scope, data.organization_id, user)
    if data.project_id is not None:
        await _assert_project_visible(db, data.project_id, user)

    payload = data.model_dump()
    payload["stages"] = validate_saved_pipeline(payload["stages"])
    make_default = data.is_default
    if make_default:
        payload["is_default"] = False
    pipeline = ProjectPipeline(
        id=uuid.uuid4(),
        created_by=user.id,
        **payload,
    )
    if make_default and pipeline.project_id is not None:
        await switch_project_default_pipeline(db, pipeline.project_id)
        pipeline.is_default = True
    db.add(pipeline)
    await db.flush()
    return pipeline


async def _load_pipeline_or_404(
    db: AsyncSession, pipeline_id: uuid.UUID
) -> ProjectPipeline:
    pipeline = await db.get(ProjectPipeline, pipeline_id)
    if pipeline is None:
        raise HTTPException(status_code=404, detail="编排不存在")
    return pipeline


async def update_pipeline(
    db: AsyncSession,
    pipeline_id: uuid.UUID,
    data: ProjectPipelineUpdate,
    user: IdentityActorFacts,
) -> ProjectPipeline:
    pipeline = await _load_pipeline_or_404(db, pipeline_id)
    await assert_pipeline_visible(db, pipeline, user)
    if not await can_edit_pipeline(db, pipeline, user):
        raise HTTPException(status_code=403, detail="无权编辑该编排")

    payload = data.model_dump(exclude_unset=True)
    scope = payload.get("scope", pipeline.scope)
    project_id = payload.get("project_id", pipeline.project_id)
    organization_id = payload.get("organization_id", pipeline.organization_id)
    try:
        _validate_scope_owner(scope, project_id, organization_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await assert_can_create_pipeline_scope(db, scope, organization_id, user)
    if project_id is not None:
        await _assert_project_visible(db, project_id, user)
    if payload.get("stages") is not None:
        payload["stages"] = validate_saved_pipeline(payload["stages"])
    # Moving a default to another project must not implicitly displace that
    # project's current default unless the caller explicitly requests it.
    if (
        project_id is not None
        and project_id != pipeline.project_id
        and "is_default" not in payload
    ):
        payload["is_default"] = False
    next_is_default = payload.get("is_default", pipeline.is_default)
    if scope != "private" and next_is_default:
        raise HTTPException(status_code=422, detail="只有 private 项目编排可以设为默认")
    if (
        next_is_default
        and project_id is not None
        and (payload.get("is_default") is True or project_id != pipeline.project_id)
    ):
        await switch_project_default_pipeline(db, project_id)

    for key, value in payload.items():
        setattr(pipeline, key, value)
    await db.flush()
    return pipeline


async def delete_pipeline(
    db: AsyncSession, pipeline_id: uuid.UUID, user: IdentityActorFacts
) -> None:
    pipeline = await _load_pipeline_or_404(db, pipeline_id)
    await assert_pipeline_visible(db, pipeline, user)
    if not await can_edit_pipeline(db, pipeline, user):
        raise HTTPException(status_code=403, detail="无权删除该编排")
    await db.delete(pipeline)


async def apply_pipeline(
    db: AsyncSession,
    *,
    source: ProjectPipeline,
    project_id: uuid.UUID,
    creator_id: uuid.UUID,
    canonical_stages: list[dict] | None,
    set_default: bool,
) -> ProjectPipeline:
    copied = ProjectPipeline(
        id=uuid.uuid4(),
        scope="private",
        project_id=project_id,
        organization_id=None,
        name=source.name,
        stages=copy_pipeline_stages(canonical_stages),
        is_default=False,
        created_by=creator_id,
    )
    # Keep the usage increment atomic with the copied row and default switch.
    await db.execute(
        update(ProjectPipeline)
        .where(ProjectPipeline.id == source.id)
        .values(usage_count=func.coalesce(ProjectPipeline.usage_count, 0) + 1)
    )
    if set_default:
        await switch_project_default_pipeline(db, project_id)
        copied.is_default = True
    db.add(copied)
    await db.flush()
    return copied
