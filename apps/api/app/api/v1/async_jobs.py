"""v0.10.16 · 统一异步任务表 API（ROADMAP §1.7）。

GET /async-jobs        列表（仅 owner 可见，super_admin 可见全部）
GET /async-jobs/{id}   详情
POST /async-jobs/{id}/cancel  软取消
"""

from __future__ import annotations

import uuid
from typing import Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.async_job_contracts import (
    AsyncJobOperationFailure,
    AsyncJobReadPort,
    JobCancellationPort,
    PredictionRetryPort,
    RetryCredentialAuthority,
)
from app.deps import (
    get_async_job_read_port,
    get_current_user_id,
    get_db,
    get_job_cancellation_port,
    get_prediction_retry_port,
)
from app.schemas.async_job import (
    AsyncJobCancelResponse,
    AsyncJobListResponse,
    AsyncJobOut,
    AsyncJobRetryFailedResponse,
)

router = APIRouter()

AsyncJobStatusParam = Literal["pending", "running", "completed", "failed", "cancelled"]


def _raise_operation_failure(exc: AsyncJobOperationFailure) -> NoReturn:
    raise HTTPException(
        status_code=exc.status_code,
        detail=exc.detail,
        headers=dict(exc.headers) if exc.headers is not None else None,
    ) from exc


@router.get("/async-jobs", response_model=AsyncJobListResponse)
async def list_async_jobs(
    status: list[AsyncJobStatusParam] | None = Query(default=None),
    kind: list[str] | None = Query(default=None),
    project_id: uuid.UUID | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    actor_id: uuid.UUID = Depends(get_current_user_id),
    read_port: AsyncJobReadPort = Depends(get_async_job_read_port),
) -> AsyncJobListResponse:
    """v0.10.16 · 当前用户可见 async_jobs 列表（super_admin 可见全部）。

    顺序：created_at DESC。前端铃铛走 polling，默认拉最近 50 行。
    """
    return await read_port.list_jobs(
        actor_id=actor_id,
        status=status,
        kind=kind,
        project_id=project_id,
        search=search,
        limit=limit,
        offset=offset,
    )


@router.get("/async-jobs/{job_id}", response_model=AsyncJobOut)
async def get_async_job(
    job_id: uuid.UUID,
    actor_id: uuid.UUID = Depends(get_current_user_id),
    read_port: AsyncJobReadPort = Depends(get_async_job_read_port),
) -> AsyncJobOut:
    try:
        result = await read_port.get_job(job_id=job_id, actor_id=actor_id)
    except AsyncJobOperationFailure as exc:
        _raise_operation_failure(exc)
    if result is None:
        raise HTTPException(status_code=404, detail="async_job not found")
    return result


@router.post("/async-jobs/{job_id}/cancel", response_model=AsyncJobCancelResponse)
async def cancel_async_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
    cancellation: JobCancellationPort = Depends(get_job_cancellation_port),
) -> AsyncJobCancelResponse:
    """v0.10.51 · 软取消。

    batch_predict 走协作取消：写 cancel_requested 标记并 revoke(terminate=False)，
    worker 在下一条预测边界落 cancelled 终态；video_tracker 仍走自身取消路径。
    """
    try:
        status = await cancellation.cancel(job_id=job_id, actor_id=actor_id)
    except AsyncJobOperationFailure as exc:
        _raise_operation_failure(exc)
    await db.commit()
    return AsyncJobCancelResponse(status=status, id=job_id)


@router.post(
    "/async-jobs/{job_id}/retry-failed",
    status_code=202,
    response_model=AsyncJobRetryFailedResponse,
)
async def retry_failed_async_job_items(
    job_id: uuid.UUID,
    request: Request,
    actor_id: uuid.UUID = Depends(get_current_user_id),
    retry_port: PredictionRetryPort = Depends(get_prediction_retry_port),
) -> AsyncJobRetryFailedResponse:
    try:
        return await retry_port.retry_job_items(
            job_id=job_id,
            actor_id=actor_id,
            credential=RetryCredentialAuthority.from_request(actor_id, request),
        )
    except AsyncJobOperationFailure as exc:
        _raise_operation_failure(exc)
