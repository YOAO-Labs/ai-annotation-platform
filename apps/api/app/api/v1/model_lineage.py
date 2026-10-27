"""Manager-only bounded model provenance and append-only decisions."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.training_claims import project_lock
from app.services.project_write_guard import lock_project_manager
from app.db.models.project import Project
from app.deps import get_current_user, get_db, require_project_owner, require_scopes
from app.schemas.model_lineage import (
    LineageOut,
    ModelDecisionCreate,
    ModelDecisionOut,
    ModelDecisionPage,
    NodeKind,
)
from app.services import model_lineage as service
from app.services.identity_contracts import IdentityActorFacts

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
    limit: int = Query(20, ge=1, le=100),
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    return await service.lineage(db, project.id, kind, node_id, relation, cursor, limit)


@router.get("/trained-models/{version_id}/decisions", response_model=ModelDecisionPage)
async def get_decisions(
    version_id: uuid.UUID,
    before: int | None = Query(None, ge=1),
    limit: int = Query(20, ge=1, le=100),
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
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await project_lock(db, project.id)
    await lock_project_manager(db, user.id, project.id)
    item = await service.append_decision(db, project.id, version_id, user, body)
    await db.commit()
    return item
