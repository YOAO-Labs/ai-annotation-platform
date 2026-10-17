"""Manager observations and separately authenticated executor append protocol."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.training_runs import executor_auth
from app.db.models.project import Project
from app.db.models.training_run import TrainingAttempt
from app.deps import (
    get_db,
    get_training_authority,
    require_project_owner,
    require_scopes,
)
from app.schemas.training_observability import (
    ExecutorObservations,
    LogPage,
    ObservationAck,
    TelemetryPage,
    TrainingRecovery,
)
from app.services import training_observability as service
from app.services import training_runs as run_service
from app.services.training_authority_contract import TrainingAuthorityPort

router = APIRouter(
    prefix="/projects/{project_id}/training-runs",
    dependencies=[Depends(require_scopes("annotations:read", "datasets:read"))],
)
executor_router = APIRouter(
    prefix="/internal/training", dependencies=[Depends(executor_auth)]
)


async def load_attempt(db, project_id, run_id, attempt_id):
    run = await run_service.load_run(db, project_id, run_id)
    attempt = await db.get(TrainingAttempt, attempt_id)
    if attempt is None or attempt.run_id != run.id:
        raise HTTPException(status_code=404, detail="执行尝试不存在")
    return attempt


@router.get("/{run_id}/attempts/{attempt_id}/telemetry", response_model=TelemetryPage)
async def telemetry(
    run_id: uuid.UUID,
    attempt_id: uuid.UUID,
    after: int | None = Query(default=None, ge=0, le=2147483647),
    limit: int = Query(default=120, ge=1, le=120),
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    attempt = await load_attempt(db, project.id, run_id, attempt_id)
    return await service.page(db, attempt, "telemetry", after=after, limit=limit)


@router.get("/{run_id}/attempts/{attempt_id}/logs", response_model=LogPage)
async def logs(
    run_id: uuid.UUID,
    attempt_id: uuid.UUID,
    after: int | None = Query(default=None, ge=0, le=2147483647),
    before: int | None = Query(default=None, ge=1, le=2147483647),
    limit: int = Query(default=120, ge=1, le=120),
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    if after is not None and before is not None:
        raise HTTPException(status_code=422, detail="日志分页不能同时指定前后游标")
    attempt = await load_attempt(db, project.id, run_id, attempt_id)
    return await service.page(
        db, attempt, "logs", after=after, before=before, limit=limit
    )


@router.get("/{run_id}/recovery", response_model=TrainingRecovery)
async def recovery(
    run_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    training_authority: TrainingAuthorityPort = Depends(get_training_authority),
):
    run = await run_service.load_run(db, project.id, run_id)
    return await service.recovery(db, run, training_authority=training_authority)


@executor_router.post(
    "/attempts/{attempt_id}/observations", response_model=ObservationAck
)
async def observations(
    attempt_id: uuid.UUID,
    body: ExecutorObservations,
    db: AsyncSession = Depends(get_db),
    training_authority: TrainingAuthorityPort = Depends(get_training_authority),
):
    result = await service.ingest(
        db, attempt_id, body, training_authority=training_authority
    )
    await db.commit()
    return result
