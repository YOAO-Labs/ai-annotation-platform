"""Project-managed task choices and non-mutating frozen-contract diagnostics."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.db.models.training_dataset import TrainingDatasetVersion
from app.deps import get_db, require_project_owner, require_scopes
from app.schemas.project_task_dataset import (
    ProjectTaskDatasetPage,
    UnrecognizedTrainingDatasetVersionPage,
)
from app.services.project_task_dataset import (
    list_task_datasets,
    recognized_task_expression,
)

router = APIRouter(
    prefix="/projects/{project_id}/task-datasets",
    dependencies=[Depends(require_scopes("annotations:read", "datasets:read"))],
)


@router.get("", response_model=ProjectTaskDatasetPage)
async def list_project_task_datasets(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    return await list_task_datasets(db, project)


@router.get(
    "/unrecognized-versions", response_model=UnrecognizedTrainingDatasetVersionPage
)
async def list_unrecognized_versions(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    stmt = select(
        TrainingDatasetVersion.id,
        TrainingDatasetVersion.name,
        TrainingDatasetVersion.status,
        TrainingDatasetVersion.created_at,
        case_raw_task(),
    ).where(
        TrainingDatasetVersion.project_id == project.id,
        recognized_task_expression().is_(None),
    )
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        (
            await db.execute(
                stmt.order_by(
                    TrainingDatasetVersion.created_at.desc(),
                    TrainingDatasetVersion.id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        )
        .mappings()
        .all()
    )
    return {"items": [dict(row) for row in rows], "total": total}


def case_raw_task():
    from sqlalchemy import case

    value = TrainingDatasetVersion.snapshot["task"]
    return case(
        (func.jsonb_typeof(value) == "string", value.as_string()), else_=None
    ).label("raw_task")
