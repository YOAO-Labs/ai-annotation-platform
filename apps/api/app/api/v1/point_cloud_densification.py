"""Project-manager preparation controls; annotators consume immutable assets."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.db.models.user import User
from app.deps import (
    get_current_user,
    get_db,
    require_active_task_actor,
    require_project_capability,
    require_project_visible,
    require_scopes,
)
from app.schemas.point_cloud_densification import (
    PointCloudDensificationRequest,
    PointCloudPreparationResponse,
    PointCloudPreparationOptions,
)
from app.services import async_job as jobs
from app.services.async_job_notify import notify_job_terminal
from app.services.point_cloud_auxiliary import AuxiliaryError
from app.services.point_cloud_densification.service import (
    create_preparation,
    list_preparation_options,
)
from app.services.project_access import (
    ProjectAccess,
    ProjectCapability,
    assert_capability,
    resolve_project_access,
)
from app.services.project_write_guard import lock_actor_project_authority

router = APIRouter(dependencies=[Depends(require_active_task_actor)])


@router.get(
    "/projects/{project_id}/point-cloud/augmentation/preparation",
    response_model=PointCloudPreparationOptions,
    dependencies=[Depends(require_scopes("annotations:read"))],
)
async def preparation_options(
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.PROJECT_MANAGE.value)
    ),
):
    return await list_preparation_options(db, project)


@router.post(
    "/projects/{project_id}/point-cloud/augmentation/jobs",
    response_model=PointCloudPreparationResponse,
    status_code=202,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def prepare_scene(
    body: PointCloudDensificationRequest,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.PROJECT_MANAGE.value)
    ),
):
    await lock_actor_project_authority(db, current_user.id, project.id)
    access = await resolve_project_access(
        db, user=current_user, project=project, lock_membership=True
    )
    assert_capability(access, ProjectCapability.PROJECT_MANAGE.value)
    try:
        job, created = await create_preparation(db, project, current_user.id, body)
    except AuxiliaryError as exc:
        raise HTTPException(409, detail={"reason": exc.code}) from exc
    await db.commit()
    if created:
        from app.workers.celery_app import celery_app

        try:
            celery_app.send_task(
                "app.workers.point_cloud_densification.run",
                args=[str(job.id)],
                task_id=job.celery_task_id,
            )
        except Exception as exc:
            await jobs.mark_failed(db, job.id, error="preparation_dispatch_failed")
            job.completed_at = datetime.now(timezone.utc)
            await notify_job_terminal(db, job_id=job.id)
            await db.commit()
            raise HTTPException(
                503, detail={"reason": "preparation_dispatch_failed"}
            ) from exc
    return PointCloudPreparationResponse(
        job_id=job.id,
        status=job.status,
        total_tasks=job.payload["total_tasks"],
        reused=not created,
    )
