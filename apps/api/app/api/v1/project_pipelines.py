"""v0.21.0 · 命名项目编排库端点."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.projects import _validate_saved_pipeline
from app.db.enums import UserRole
from app.db.models.project_pipeline import ProjectPipeline
from app.db.models.user import User
from app.deps import assert_project_visible, get_current_user, get_db
from app.schemas.project_pipeline import (
    ProjectPipelineCreate,
    ProjectPipelineOut,
    ProjectPipelineUpdate,
    _validate_scope_owner,
)
from app.services.pipeline_template import (
    assert_model_reference_scope,
    assert_can_create_pipeline_scope,
    assert_pipeline_visible,
    can_edit_pipeline,
    list_user_org_ids,
    switch_project_default_pipeline,
)

router = APIRouter()


async def _serialize(
    db: AsyncSession,
    pipeline: ProjectPipeline,
    creator_name_map: dict[uuid.UUID, str | None] | None = None,
) -> dict:
    creator_name: str | None = None
    if pipeline.created_by:
        if creator_name_map is not None:
            # 批量场景 (list): 名字已一次性查好, 不再逐条往返 (避免 N+1)。
            creator_name = creator_name_map.get(pipeline.created_by)
        else:
            row = await db.execute(
                select(User.name).where(User.id == pipeline.created_by)
            )
            creator_name = row.scalar_one_or_none()
    data = {c.name: getattr(pipeline, c.name) for c in pipeline.__table__.columns}
    data["created_by_name"] = creator_name
    return data


async def _load_pipeline_or_404(
    db: AsyncSession, pipeline_id: uuid.UUID
) -> ProjectPipeline:
    pipeline = await db.get(ProjectPipeline, pipeline_id)
    if pipeline is None:
        raise HTTPException(status_code=404, detail="编排不存在")
    return pipeline


@router.get("", response_model=list[ProjectPipelineOut])
async def list_pipelines(
    scope: Annotated[
        str | None, Query(pattern="^(private|organization|public)$")
    ] = None,
    project_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = select(ProjectPipeline)
    if user.role != UserRole.SUPER_ADMIN:
        org_ids = await list_user_org_ids(db, user)
        visibility_clauses = [
            ProjectPipeline.scope == "public",
            and_(
                ProjectPipeline.scope == "private",
                ProjectPipeline.created_by == user.id,
            ),
        ]
        if org_ids:
            visibility_clauses.append(
                and_(
                    ProjectPipeline.scope == "organization",
                    ProjectPipeline.organization_id.in_(org_ids),
                )
            )
        q = q.where(or_(*visibility_clauses))
    if scope:
        q = q.where(ProjectPipeline.scope == scope)
    if project_id:
        q = q.where(ProjectPipeline.project_id == project_id)
    if organization_id:
        q = q.where(ProjectPipeline.organization_id == organization_id)
    rows = await db.execute(q.order_by(ProjectPipeline.created_at.desc()))
    pipelines = rows.scalars().all()
    # 一次性取全部创建者名字, 消除 _serialize 里的逐条 SELECT (N+1)。
    creator_ids = {p.created_by for p in pipelines if p.created_by}
    creator_name_map: dict[uuid.UUID, str | None] = {}
    if creator_ids:
        name_rows = await db.execute(
            select(User.id, User.name).where(User.id.in_(creator_ids))
        )
        creator_name_map = {uid: uname for uid, uname in name_rows.all()}
    return [await _serialize(db, p, creator_name_map) for p in pipelines]


@router.post("", response_model=ProjectPipelineOut, status_code=201)
async def create_pipeline(
    data: ProjectPipelineCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role not in (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN):
        raise HTTPException(
            status_code=403, detail="仅项目管理员或超级管理员可创建编排"
        )
    await assert_can_create_pipeline_scope(db, data.scope, data.organization_id, user)
    if data.project_id is not None:
        await assert_project_visible(data.project_id, db, user)
    data.stages = _validate_saved_pipeline(data.stages)
    await assert_model_reference_scope(db, data.stages, data.scope, data.project_id)

    make_default = data.is_default
    payload = data.model_dump()
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
    await db.commit()
    await db.refresh(pipeline)
    return await _serialize(db, pipeline)


@router.get("/{pipeline_id}", response_model=ProjectPipelineOut)
async def get_pipeline(
    pipeline_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    pipeline = await _load_pipeline_or_404(db, pipeline_id)
    await assert_pipeline_visible(db, pipeline, user)
    return await _serialize(db, pipeline)


@router.put("/{pipeline_id}", response_model=ProjectPipelineOut)
async def update_pipeline(
    pipeline_id: uuid.UUID,
    data: ProjectPipelineUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
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
        await assert_project_visible(project_id, db, user)
    if payload.get("stages") is not None:
        payload["stages"] = _validate_saved_pipeline(payload["stages"])
    await assert_model_reference_scope(
        db, payload.get("stages", pipeline.stages), scope, project_id
    )
    # 跨项目搬迁且未显式给 is_default 时, 不继承 default 身份: 换归属不应隐式改「谁是默认」,
    # 否则会静默降级目标项目既有的默认编排。要设为新项目默认须显式传 is_default=true。
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

    for k, v in payload.items():
        setattr(pipeline, k, v)
    await db.flush()
    await db.commit()
    await db.refresh(pipeline)
    return await _serialize(db, pipeline)


@router.delete("/{pipeline_id}", status_code=204)
async def delete_pipeline(
    pipeline_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    pipeline = await _load_pipeline_or_404(db, pipeline_id)
    await assert_pipeline_visible(db, pipeline, user)
    if not await can_edit_pipeline(db, pipeline, user):
        raise HTTPException(status_code=403, detail="无权删除该编排")
    await db.delete(pipeline)
    await db.commit()
