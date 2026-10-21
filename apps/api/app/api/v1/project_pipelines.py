"""Saved inference-pipeline API routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.schemas.project_pipeline import (
    ProjectPipelineCreate,
    ProjectPipelineOut,
    ProjectPipelineUpdate,
)
from app.services.identity_contracts import IdentityActorFacts
from app.services.inference_pipelines import (
    commit_pipeline_command,
    execute_pipeline_command,
    get_pipeline_projection,
    get_pipeline_row,
    list_pipeline_rows,
)

router = APIRouter()


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
    return await list_pipeline_rows(
        db,
        actor=user,
        scope=scope,
        project_id=project_id,
        organization_id=organization_id,
    )


@router.post("", response_model=ProjectPipelineOut, status_code=201)
async def create_pipeline(
    data: ProjectPipelineCreate,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    mutation = await execute_pipeline_command(
        db,
        operation="create_pipeline",
        actor=user,
        payload=data,
    )
    await commit_pipeline_command(db, mutation)
    return await get_pipeline_projection(db, mutation, actor=user)


@router.get("/{pipeline_id}", response_model=ProjectPipelineOut)
async def get_pipeline(
    pipeline_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    return await get_pipeline_row(db, actor=user, pipeline_id=pipeline_id)


@router.put("/{pipeline_id}", response_model=ProjectPipelineOut)
async def update_pipeline(
    pipeline_id: uuid.UUID,
    data: ProjectPipelineUpdate,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    mutation = await execute_pipeline_command(
        db,
        operation="update_pipeline",
        actor=user,
        target_id=pipeline_id,
        payload=data,
    )
    await commit_pipeline_command(db, mutation)
    return await get_pipeline_projection(db, mutation, actor=user)


@router.delete("/{pipeline_id}", status_code=204)
async def delete_pipeline(
    pipeline_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    mutation = await execute_pipeline_command(
        db,
        operation="delete_pipeline",
        actor=user,
        target_id=pipeline_id,
        payload={},
    )
    await commit_pipeline_command(db, mutation)
