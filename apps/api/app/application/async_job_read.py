"""SQL binding for session-free asynchronous job reads."""

from __future__ import annotations

import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.async_job_contracts import AsyncJobOperationFailure
from app.db.enums import PlatformRole, ProjectRole
from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.db.models.user import User
from app.schemas.async_job import AsyncJobListResponse, AsyncJobOut
from app.services import async_job_access
from app.services.project_aggregates import project_scope_clause


def _build_async_job_query(
    *,
    current_user: User,
    status: list[str] | None,
    kind: list[str] | None,
    project_id: uuid.UUID | None,
    search: str | None,
):
    """Build the one scoped async-job relation used by page and count queries."""
    project_scope_id = func.coalesce(AsyncJob.project_id, AsyncJob.retained_project_id)
    project_exists = (
        select(Project.id)
        .where(Project.id == project_scope_id)
        .correlate(AsyncJob)
        .exists()
    )
    unresolved_scope = and_(
        project_scope_id.is_(None),
        AsyncJob.kind.not_in(async_job_access.GLOBAL_ASYNC_JOB_KINDS),
    )
    query = select(AsyncJob).where(
        ~unresolved_scope,
        or_(project_scope_id.is_(None), project_exists),
    )

    if current_user.role != PlatformRole.SUPER_ADMIN.value:
        # Resolve membership in SQL so page and count scopes need no per-job read.
        project_scope = project_scope_clause(
            current_user, project_scope_id, project_roles=()
        )
        export_scope = project_scope_clause(
            current_user,
            project_scope_id,
            project_roles=(ProjectRole.REVIEWER.value,),
        )
        query = query.where(
            AsyncJob.user_id == current_user.id,
            or_(
                and_(
                    AsyncJob.kind != async_job_access.EXPORT_JOB_KIND,
                    project_scope_id.is_(None),
                ),
                project_scope,
            ),
            or_(
                AsyncJob.kind != async_job_access.EXPORT_JOB_KIND,
                and_(project_scope_id.is_not(None), export_scope),
            ),
        )
    if status:
        query = query.where(AsyncJob.status.in_(status))
    if kind:
        query = query.where(AsyncJob.kind.in_(kind))
    if project_id:
        query = query.where(project_scope_id == project_id)

    search_text = search.strip() if search else ""
    if search_text:
        pattern = f"%{search_text}%"
        query = query.where(
            or_(
                AsyncJob.payload["prompt"].astext.ilike(pattern),
                AsyncJob.payload["batch_display_id"].astext.ilike(pattern),
                AsyncJob.payload["task_display_id"].astext.ilike(pattern),
                AsyncJob.payload["model_key"].astext.ilike(pattern),
                AsyncJob.payload["ml_backend_name"].astext.ilike(pattern),
                AsyncJob.payload["error_type"].astext.ilike(pattern),
            )
        )
    return query


async def _to_async_job_out(
    db: AsyncSession,
    job: AsyncJob,
    project_cache: dict[uuid.UUID, Project | None] | None = None,
) -> AsyncJobOut:
    project: Project | None = None
    project_scope_id = job.project_id or job.retained_project_id
    if project_scope_id is not None:
        if project_cache is None:
            project = await db.get(Project, project_scope_id)
        else:
            if project_scope_id not in project_cache:
                project_cache[project_scope_id] = await db.get(
                    Project, project_scope_id
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
    ) -> AsyncJobListResponse:
        user = await self._db.get(User, actor_id, populate_existing=True)
        if user is None or not user.is_active:
            return AsyncJobListResponse(items=[], total=0)

        stmt = _build_async_job_query(
            current_user=user,
            status=status,
            kind=kind,
            project_id=project_id,
            search=search,
        )
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        stmt = (
            stmt.order_by(AsyncJob.created_at.desc(), AsyncJob.id.desc())
            .offset(offset)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        total = (await self._db.execute(count_stmt)).scalar_one()
        project_cache: dict[uuid.UUID, Project | None] = {}

        return AsyncJobListResponse(
            items=[
                await _to_async_job_out(self._db, row, project_cache) for row in rows
            ],
            total=total,
        )

    async def get_job(
        self, *, job_id: uuid.UUID, actor_id: uuid.UUID
    ) -> AsyncJobOut | None:
        job = await self._db.get(AsyncJob, job_id, populate_existing=True)
        if job is None:
            return None
        if not await async_job_access._can_access_job(
            self._db, job=job, actor_id=actor_id
        ):
            raise AsyncJobOperationFailure(403, "not your job")
        return await _to_async_job_out(self._db, job)
