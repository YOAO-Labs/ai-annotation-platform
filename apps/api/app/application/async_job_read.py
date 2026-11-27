"""SQL binding for session-free asynchronous job reads."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from yoao_jobs.async_job_list_query_contract import AsyncJobListQuery
from app.application.async_job_contracts import AsyncJobOperationFailure
from app.db.enums import PlatformRole
from app.services.project_access import get_workspace_project
from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.schemas.async_job import AsyncJobListResponse, AsyncJobOut
from app.services import (
    jobs_lifecycle_data as jobs,
    async_job_access,
    storage_maintenance,
)
from app.services.jobs_list_query_data import JobsListQueryPostgresData


async def _to_async_job_out(
    db: AsyncSession,
    job: AsyncJob,
    project_cache: dict[uuid.UUID, Project | None] | None = None,
) -> AsyncJobOut:
    project: Project | None = None
    project_scope_id = job.project_id or job.retained_project_id
    if project_scope_id is not None:
        if project_cache is None:
            project = await get_workspace_project(db, project_id=project_scope_id)
        else:
            if project_scope_id not in project_cache:
                project_cache[project_scope_id] = await get_workspace_project(
                    db, project_id=project_scope_id
                )
            project = project_cache[project_scope_id]

    payload_project_display_id = None
    if isinstance(job.payload, dict):
        raw_display_id = job.payload.get("project_display_id")
        if isinstance(raw_display_id, str):
            payload_project_display_id = raw_display_id

    return AsyncJobOut.model_validate(job).model_copy(
        update={
            "project_id": project_scope_id,
            "project_display_id": (
                project.display_id if project else payload_project_display_id
            ),
            "project_name": project.name if project else None,
        }
    )


class SqlAsyncJobReadAdapter:
    """Bind the read port to the request session and keep ORM rows private."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_jobs(
        self,
        *,
        actor_id: uuid.UUID,
        status: list[str] | None,
        kind: list[str] | None,
        project_id: uuid.UUID | None,
        search: str | None,
        limit: int,
        offset: int,
        api_key_id: uuid.UUID | None = None,
        api_key_scopes: tuple[str, ...] | None = None,
    ) -> AsyncJobListResponse:
        user = await IdentityAccountLockData(self._db).read_actor_status(actor_id)
        if user is None or not user.is_active:
            return AsyncJobListResponse(items=[], total=0)

        include_platform_storage = False
        if user.role == PlatformRole.SUPER_ADMIN.value:
            has_wildcard_scope = api_key_scopes is None or "*" in api_key_scopes
            if api_key_id is None:
                include_platform_storage = has_wildcard_scope
            elif has_wildcard_scope:
                try:
                    await storage_maintenance.authority(self._db, user, api_key_id, "*")
                except HTTPException:
                    include_platform_storage = False
                else:
                    include_platform_storage = True

        include_dataset_jobs = False
        if user.role in {
            PlatformRole.SUPER_ADMIN.value,
            PlatformRole.PROJECT_ADMIN.value,
        }:
            has_dataset_read_scope = (
                api_key_scopes is None
                or "*" in api_key_scopes
                or "datasets:read" in api_key_scopes
            )
            if api_key_id is None:
                include_dataset_jobs = has_dataset_read_scope
            elif has_dataset_read_scope:
                include_dataset_jobs = await IdentityAccountLockData(
                    self._db
                ).accepted_api_key_authorized(actor_id, api_key_id, ("datasets:read",))

        search_text = search.strip() if search else ""
        request = AsyncJobListQuery(
            actor_id=user.id,
            platform_role=user.role,
            statuses=status or [],
            kinds=kind or [],
            project_id=project_id,
            search_pattern=f"%{search_text}%" if search_text else None,
            limit=limit,
            offset=offset,
        )
        query_data = JobsListQueryPostgresData(self._db, AsyncJob)
        rows = await query_data.page(
            request,
            include_platform_storage=include_platform_storage,
            include_dataset_jobs=include_dataset_jobs,
        )
        total = await query_data.total(
            request,
            include_platform_storage=include_platform_storage,
            include_dataset_jobs=include_dataset_jobs,
        )
        project_cache: dict[uuid.UUID, Project | None] = {}

        return AsyncJobListResponse(
            items=[
                await _to_async_job_out(self._db, row, project_cache) for row in rows
            ],
            total=total,
        )

    async def get_job(
        self,
        *,
        job_id: uuid.UUID,
        actor_id: uuid.UUID,
        api_key_id: uuid.UUID | None = None,
        api_key_scopes: tuple[str, ...] | None = None,
    ) -> AsyncJobOut | None:
        job = await jobs.get_job(self._db, job_id, populate_existing=True)
        if job is None:
            return None
        if not await async_job_access._can_access_job(
            self._db, job=job, actor_id=actor_id
        ):
            raise AsyncJobOperationFailure(403, "not your job")
        if job.kind in async_job_access.PLATFORM_STORAGE_ADMIN_JOB_KINDS:
            if api_key_scopes is not None and "*" not in api_key_scopes:
                raise AsyncJobOperationFailure(
                    403,
                    "API key 缺少所需权限: *",
                    headers={"WWW-Authenticate": 'Bearer scope="*"'},
                )
            if api_key_id is not None:
                actor = await IdentityAccountLockData(self._db).read_actor_status(
                    actor_id
                )
                if actor is None or not actor.is_active:
                    raise AsyncJobOperationFailure(403, "not your job")
                try:
                    await storage_maintenance.authority(
                        self._db, actor, api_key_id, "*"
                    )
                except HTTPException as exc:
                    raise AsyncJobOperationFailure(
                        exc.status_code, exc.detail, headers=exc.headers
                    ) from exc
        elif job.kind in async_job_access.DATASET_ASYNC_JOB_KINDS:
            if (
                api_key_scopes is not None
                and "*" not in api_key_scopes
                and "datasets:read" not in api_key_scopes
            ):
                raise AsyncJobOperationFailure(
                    403,
                    "API key 缺少所需权限: datasets:read",
                    headers={"WWW-Authenticate": 'Bearer scope="datasets:read"'},
                )
            if api_key_id is not None:
                authorized = await IdentityAccountLockData(
                    self._db
                ).accepted_api_key_authorized(actor_id, api_key_id, ("datasets:read",))
                if not authorized:
                    raise AsyncJobOperationFailure(
                        403, {"code": "api_key_authority_revoked"}
                    )
        return await _to_async_job_out(self._db, job)
