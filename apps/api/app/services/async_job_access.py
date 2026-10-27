"""Current project and task visibility for stored asynchronous job results."""

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import PlatformRole, ProjectRole
from app.services.project_access import get_workspace_project
from app.db.models.async_job import AsyncJob, AsyncJobKind
from app.services import async_job as jobs, task_access
from app.services.identity_account_lock_data import IdentityAccountLockData
from aap_workspace_contract.access import ProjectCapability
from app.services.project_access import resolve_project_access

EXPORT_JOB_KIND = "export"
# Unlisted kinds require project scope; these are the audited global production kinds.
GLOBAL_ASYNC_JOB_KINDS = frozenset(
    {AsyncJobKind.AUDIT_ARCHIVE.value, AsyncJobKind.DATASET_IMPORT.value}
)


async def _can_access_job(
    db: AsyncSession, *, job: AsyncJob, actor_id: uuid.UUID
) -> bool:
    """Reauthorize job ownership and frozen task scope against current project access."""
    job = await jobs.get_job(db, job.id, populate_existing=True)
    if job is None:
        return False
    actor = await IdentityAccountLockData(db).read_actor_status(actor_id)
    if actor is None or not actor.is_active:
        return False
    if job.kind in {
        "storage_usage",
        "managed_original_verification",
        "managed_original_reclaim",
        "managed_reference_reconciliation",
        "storage_migration_inventory",
        "storage_migration_copy",
    }:
        return actor.role == PlatformRole.SUPER_ADMIN.value
    if job.kind in {"dataset_quality", "dataset_quality_review"}:
        from app.services import (
            dataset_quality as quality_svc,
            dataset_quality_review as review_svc,
            dataset_source_access as source_access,
        )
        from sqlalchemy import select

        try:
            manager = await source_access.require_manager(db, actor.id)
        except HTTPException:
            return False
        return bool(
            await db.scalar(
                select(AsyncJob.id).where(
                    AsyncJob.id == job.id,
                    (
                        review_svc
                        if job.kind == "dataset_quality_review"
                        else quality_svc
                    ).job_scope_clause(manager),
                )
            )
        )
    if job.kind in {"dataset_scan", "dataset_registration"}:
        from app.services import dataset_source_access as source_access
        from sqlalchemy import select

        try:
            manager = await source_access.require_manager(db, actor.id)
        except HTTPException:
            return False
        return bool(
            await db.scalar(
                select(AsyncJob.id).where(
                    AsyncJob.id == job.id, source_access.job_scope_clause(manager)
                )
            )
        )
    project_id = job.project_id or job.retained_project_id
    if project_id is None:
        if job.kind == EXPORT_JOB_KIND:
            return False
        if job.kind not in GLOBAL_ASYNC_JOB_KINDS:
            return False
        if actor.role == PlatformRole.SUPER_ADMIN.value:
            return True
        return job.user_id == actor.id
    project = await get_workspace_project(
        db, project_id=project_id, populate_existing=True
    )
    if project is None:
        return False
    if actor.role == PlatformRole.SUPER_ADMIN.value:
        return True
    try:
        access = await resolve_project_access(db, actor_id=actor.id, project=project)
    except HTTPException:
        return False
    if access.is_manager:
        return True
    if (
        job.kind == EXPORT_JOB_KIND
        and ProjectCapability.EXPORT_ANNOTATIONS.value not in access.capabilities
    ):
        return False
    if job.user_id == actor.id:
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
    return await task_access.visible_task_ids(db, project, actor.id, task_ids) == set(
        task_ids
    )
