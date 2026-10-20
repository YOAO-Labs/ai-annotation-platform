"""v0.21.0 · 命名项目编排库端点."""

from __future__ import annotations

import uuid
from typing import Annotated

from aap_inference import PipelineValidationError
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.project_pipeline import ProjectPipeline
from app.db.models.user import User
from app.deps import get_current_user, get_db
from app.schemas.project_pipeline import (
    ProjectPipelineCreate,
    ProjectPipelineOut,
    ProjectPipelineUpdate,
)
from app.services.pipeline_template import (
    assert_pipeline_visible,
    create_pipeline as create_pipeline_command,
    delete_pipeline as delete_pipeline_command,
    list_user_org_ids,
    update_pipeline as update_pipeline_command,
)
from app.services.identity_contracts import IdentityActorFacts

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
    user: IdentityActorFacts = Depends(get_current_user),
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
    user: IdentityActorFacts = Depends(get_current_user),
):
    try:
        pipeline = await create_pipeline_command(db, data, user)
    except PipelineValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    await db.commit()
    await db.refresh(pipeline)
    return await _serialize(db, pipeline)


@router.get("/{pipeline_id}", response_model=ProjectPipelineOut)
async def get_pipeline(
    pipeline_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    pipeline = await _load_pipeline_or_404(db, pipeline_id)
    await assert_pipeline_visible(db, pipeline, user)
    return await _serialize(db, pipeline)


@router.put("/{pipeline_id}", response_model=ProjectPipelineOut)
async def update_pipeline(
    pipeline_id: uuid.UUID,
    data: ProjectPipelineUpdate,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    try:
        pipeline = await update_pipeline_command(db, pipeline_id, data, user)
    except PipelineValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    await db.commit()
    await db.refresh(pipeline)
    return await _serialize(db, pipeline)


@router.delete("/{pipeline_id}", status_code=204)
async def delete_pipeline(
    pipeline_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    await delete_pipeline_command(db, pipeline_id, user)
    await db.commit()
