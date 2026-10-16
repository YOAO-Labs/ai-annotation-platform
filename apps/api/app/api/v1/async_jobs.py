"""v0.10.16 · 统一异步任务表 API（ROADMAP §1.7）。

GET /async-jobs        列表（仅 owner 可见，super_admin 可见全部）
GET /async-jobs/{id}   详情
POST /async-jobs/{id}/cancel  软取消
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import PlatformRole, ProjectRole
from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.db.models.user import User
from app.deps import (
    get_current_user,
    get_db,
    get_job_execution_control,
    get_prediction_retry_dispatcher,
)
from app.application import job_cancellation, prediction_retry
from app.services import async_job_access
from app.services.job_execution_control import JobExecutionControl
from app.services.prediction_retry import PredictionRetryDispatcher
from app.services.project_aggregates import project_scope_clause
from app.schemas.async_job import (
    AsyncJobListResponse,
    AsyncJobOut,
    AsyncJobRetryFailedResponse,
)

router = APIRouter()


AsyncJobStatusParam = Literal["pending", "running", "completed", "failed", "cancelled"]


def _build_async_job_query(
    *,
    current_user: User,
    status: list[AsyncJobStatusParam] | None,
    kind: list[str] | None,
    project_id: uuid.UUID | None,
    search: str | None,
):
    """Build the one scoped async-job relation used by page and count queries."""
    query = select(AsyncJob)

    if current_user.role != PlatformRole.SUPER_ADMIN.value:
        # Restrict to the caller's own jobs *and* a project they may currently
        # access.  Membership/ownership is resolved in SQL (one correlated
        # subquery, no per-job lookup) and a non-administrative owner gets only
        # the membership arm.
        project_scope = project_scope_clause(
            current_user, AsyncJob.project_id, project_roles=()
        )
        # Export rows carry a signed download URL in ``result``.  The list and
        # count endpoints must apply the same export capability as the detail
        # endpoint, otherwise a downgraded account could still obtain the URL.
        export_scope = project_scope_clause(
            current_user,
            AsyncJob.project_id,
            project_roles=(ProjectRole.REVIEWER.value,),
        )
        query = query.where(
            AsyncJob.user_id == current_user.id,
            or_(
                # Genuine global non-export jobs stay visible; a global export
                # row has no legitimate mode and must fail closed.
                and_(
                    AsyncJob.kind != async_job_access.EXPORT_JOB_KIND,
                    AsyncJob.project_id.is_(None),
                ),
                project_scope,
            ),
            or_(
                AsyncJob.kind != async_job_access.EXPORT_JOB_KIND,
                and_(
                    AsyncJob.project_id.is_not(None),
                    export_scope,
                ),
            ),
        )
    if status:
        query = query.where(AsyncJob.status.in_(status))
    if kind:
        query = query.where(AsyncJob.kind.in_(kind))
    if project_id:
        query = query.where(AsyncJob.project_id == project_id)

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
    if job.project_id is not None:
        if project_cache is None:
            project = await db.get(Project, job.project_id)
        else:
            if job.project_id not in project_cache:
                project_cache[job.project_id] = await db.get(Project, job.project_id)
            project = project_cache[job.project_id]

    payload_project_display_id = None
    if isinstance(job.payload, dict):
        raw_display_id = job.payload.get("project_display_id")
        if isinstance(raw_display_id, str):
            payload_project_display_id = raw_display_id

    return AsyncJobOut.model_validate(job).model_copy(
        update={
            "project_display_id": (
                project.display_id if project else payload_project_display_id
            ),
            "project_name": project.name if project else None,
        }
    )


@router.get("/async-jobs", response_model=AsyncJobListResponse)
async def list_async_jobs(
    status: list[AsyncJobStatusParam] | None = Query(default=None),
    kind: list[str] | None = Query(default=None),
    project_id: uuid.UUID | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AsyncJobListResponse:
    """v0.10.16 · 当前用户可见 async_jobs 列表（super_admin 可见全部）。

    顺序：created_at DESC。前端铃铛走 polling，默认拉最近 50 行。
    """
    stmt = _build_async_job_query(
        current_user=current_user,
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
    )
    rows = (await db.execute(stmt)).scalars().all()
    total = (await db.execute(count_stmt)).scalar_one()
    project_cache: dict[uuid.UUID, Project | None] = {}

    return AsyncJobListResponse(
        items=[await _to_async_job_out(db, r, project_cache) for r in rows],
        total=total,
    )


@router.get("/async-jobs/{job_id}", response_model=AsyncJobOut)
async def get_async_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AsyncJobOut:
    job = await db.get(AsyncJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="async_job not found")
    if not await async_job_access.can_access_job(db, job=job, user=current_user):
        raise HTTPException(status_code=403, detail="not your job")
    return await _to_async_job_out(db, job)


@router.post("/async-jobs/{job_id}/cancel")
async def cancel_async_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    execution_control: JobExecutionControl = Depends(get_job_execution_control),
):
    """v0.10.51 · 软取消。

    batch_predict 走协作取消：写 cancel_requested 标记并 revoke(terminate=False)，
    worker 在下一条预测边界落 cancelled 终态；video_tracker 仍走自身取消路径。
    """
    status = await job_cancellation.cancel_job(
        db, job_id, current_user, execution_control=execution_control
    )
    await db.commit()
    return {"status": status, "id": str(job_id)}


@router.post(
    "/async-jobs/{job_id}/retry-failed",
    status_code=202,
    response_model=AsyncJobRetryFailedResponse,
)
async def retry_failed_async_job_items(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    dispatcher: PredictionRetryDispatcher = Depends(get_prediction_retry_dispatcher),
) -> AsyncJobRetryFailedResponse:
    return await prediction_retry.retry_job_items(
        db, job_id, current_user, dispatcher=dispatcher
    )
