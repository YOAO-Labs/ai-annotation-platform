"""Project-managed task choices and non-mutating frozen-contract diagnostics."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.deps import get_db, require_project_owner, require_scopes
from app.schemas.project_task_dataset import (
    ProjectTaskDatasetPage,
    UnrecognizedTrainingDatasetVersionPage,
)
from app.services.project_task_dataset import list_task_datasets
from app.services.training_dataset_data import dataset_lineage_row

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
    version = dataset_lineage_row.c
    stmt = select(
        version.id, version.name, version.status, version.created_at, version.raw_task
    ).where(
        version.project_id == project.id,
        version.recognized_task.is_(None),
    )
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        (
            await db.execute(
                stmt.order_by(version.created_at.desc(), version.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .mappings()
        .all()
    )
    return {"items": [dict(row) for row in rows], "total": total}
