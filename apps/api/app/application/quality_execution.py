"""Compose quality-domain execution with generic async-job state and notices."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import async_job as async_job_svc
from app.services.async_job_notify import notify_job_terminal
from app.services.mask_qc import execution as mask_qc_execution
from app.services.point_cloud_quality import execution as point_cloud_execution
from app.services.video_track_quality import (
    execute_run as execute_video_track_quality_run,
)


class _AsyncJobLifecycle:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def mark_running(
        self, job_id: uuid.UUID, *, celery_task_id: str | None
    ) -> None:
        await async_job_svc.mark_running(
            self._db, job_id, celery_task_id=celery_task_id
        )

    async def update_progress(self, job_id: uuid.UUID, progress_pct: int) -> None:
        await async_job_svc.update_progress(self._db, job_id, progress_pct)

    async def mark_complete(
        self, job_id: uuid.UUID, *, result: dict[str, Any] | None
    ) -> None:
        await async_job_svc.mark_complete(self._db, job_id, result=result)

    async def mark_failed(self, job_id: uuid.UUID, *, error: str) -> None:
        await async_job_svc.mark_failed(self._db, job_id, error=error)

    async def mark_cancelled(
        self, job_id: uuid.UUID, *, result: dict[str, Any] | None
    ) -> None:
        await async_job_svc.mark_cancelled(self._db, job_id, result=result)

    async def notify_terminal(self, *, job_id: uuid.UUID) -> None:
        await notify_job_terminal(self._db, job_id=job_id)


async def execute_mask_qc(
    db: AsyncSession, *, run_id: uuid.UUID, celery_task_id: str | None
) -> None:
    await mask_qc_execution.execute_run(
        db,
        run_id=run_id,
        celery_task_id=celery_task_id,
        job_lifecycle=_AsyncJobLifecycle(db),
    )


async def execute_point_cloud_quality(
    db: AsyncSession, *, run_id: uuid.UUID, celery_task_id: str | None
) -> None:
    await point_cloud_execution.execute_run(
        db,
        run_id=run_id,
        celery_task_id=celery_task_id,
        job_lifecycle=_AsyncJobLifecycle(db),
    )


async def execute_video_track_quality(
    db: AsyncSession, *, run_id: uuid.UUID, celery_task_id: str | None
) -> None:
    await execute_video_track_quality_run(
        db,
        run_id=run_id,
        celery_task_id=celery_task_id,
        job_lifecycle=_AsyncJobLifecycle(db),
    )
