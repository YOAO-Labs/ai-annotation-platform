"""Manager-only bounded model provenance and append-only decisions."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.trained_models import serialize
from app.api.v1.training_datasets import lock_owner
from app.db.models.project import Project
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_project_owner, require_scopes
from app.schemas.model_lineage import (
    LineageOut,
    ModelDecisionCreate,
    ModelDecisionOut,
    ModelDecisionPage,
    NodeKind,
)
from app.services import model_lineage as service

router = APIRouter(
    prefix="/projects/{project_id}",
    dependencies=[Depends(require_scopes("annotations:read", "datasets:read"))],
)


@router.get("/model-lineage/{kind}/{node_id}", response_model=LineageOut)
async def get_lineage(
    kind: NodeKind,
    node_id: uuid.UUID,
    relation: str | None = Query(None, max_length=40),
    cursor: str | None = Query(None, max_length=1000),
    limit: int = Query(20, ge=1, le=20),
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    return await service.lineage(db, project.id, kind, node_id, relation, cursor, limit)


@router.get("/trained-models/{version_id}/decisions", response_model=ModelDecisionPage)
async def get_decisions(
    version_id: uuid.UUID,
    before: int | None = Query(None, ge=1),
    limit: int = Query(20, ge=1, le=20),
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    return await service.decisions(db, project.id, version_id, before, limit)


@router.post(
    "/trained-models/{version_id}/decisions",
    response_model=ModelDecisionOut,
    status_code=201,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def create_decision(
    version_id: uuid.UUID,
    body: ModelDecisionCreate,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await serialize(db, project)
    await lock_owner(db, user, project)
    item = await service.append_decision(db, project.id, version_id, user, body)
    await db.commit()
    return item
