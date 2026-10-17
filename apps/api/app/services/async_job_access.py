"""Current project and task visibility for stored asynchronous job results."""

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import PlatformRole, ProjectRole
from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.db.models.user import User
from app.services import task_access
from app.services.workspace_contract import ProjectCapability
from app.services.project_access import resolve_project_access

EXPORT_JOB_KIND = "export"


async def _can_access_job(
    db: AsyncSession, *, job: AsyncJob, actor_id: uuid.UUID
) -> bool:
    """Reauthorize job ownership and frozen task scope against current project access."""
    user = await db.get(User, actor_id)
    if user is None:
        return False
    if user.role == PlatformRole.SUPER_ADMIN.value:
        return True
    if job.project_id is None:
        # Export has no global mode: a missing project cannot expose its signed URL.
        if job.kind == EXPORT_JOB_KIND:
            return False
        return job.user_id == user.id
    project = await db.get(Project, job.project_id)
    if project is None:
        return False
    try:
        access = await resolve_project_access(db, user=user, project=project)
    except HTTPException:
        return False
    if access.is_manager:
        return True
    if (
        job.kind == EXPORT_JOB_KIND
        and ProjectCapability.EXPORT_ANNOTATIONS.value not in access.capabilities
    ):
        return False
    if job.user_id == user.id:
        return True
    if job.kind not in {"mask_qc", "point_cloud_quality"}:
        return False
    if access.project_role != ProjectRole.REVIEWER.value:
        return False
    raw_task_ids = ((job.payload or {}).get("scope") or {}).get("task_ids") or []
    try:
        task_ids = [uuid.UUID(str(value)) for value in raw_task_ids]
    except (TypeError, ValueError):
        return False
    if not task_ids:
        return False
    return await task_access.visible_task_ids(db, project, user, task_ids) == set(
        task_ids
    )
