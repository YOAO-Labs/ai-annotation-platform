"""v0.12.0 · B4 建任务异步化 worker。

大 dataset link（item 数 > TASK_CREATE_SYNC_THRESHOLD）的建 task 从同步 HTTP
单事务搬到 Celery：分块 INSERT + 每块 commit + 进度上报，避免超时与长事务锁。
任务写入由 DatasetTaskCreationWorkflow 调用 labeling task owner。
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.db.models.async_job import AsyncJob
from app.services import async_job as async_job_svc
from app.services.async_job_notify import notify_job_terminal
from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(bind=True, name="app.workers.create_tasks.run_create_tasks")
def run_create_tasks(self, job_id: str) -> None:
    asyncio.run(
        _run_create_tasks(
            job_id=job_id,
            celery_task_id=getattr(self.request, "id", None),
        )
    )


async def _run_create_tasks(
    *,
    job_id: str,
    celery_task_id: str | None,
) -> None:
    job_uuid = uuid.UUID(job_id)
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with SessionLocal() as db:
            try:
                await async_job_svc.mark_running(
                    db, job_uuid, celery_task_id=celery_task_id
                )
                await db.commit()

                job = await db.get(AsyncJob, job_uuid)
                if job is None:
                    return
                payload = job.payload or {}
                dataset_id = uuid.UUID(str(payload["dataset_id"]))
                project_id = uuid.UUID(str(payload["project_id"]))

                # The workflow retains the link branch and commits each task chunk.
                result = await DatasetTaskCreationWorkflow(
                    db
                ).create_tasks_for_linked_dataset(
                    dataset_id=dataset_id,
                    project_id=project_id,
                    job_id=job_uuid,
                )

                # created_tasks 与前端 LinkJobProgress 读取的字段名对齐（完成 toast 显示数量）。
                await async_job_svc.mark_complete(
                    db,
                    job_uuid,
                    result={
                        "created_tasks": result["created"],
                        "total": result["total"],
                    },
                )
                await notify_job_terminal(db, job_id=job_uuid)
                await db.commit()
            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                err = f"{type(exc).__name__}: {exc}"
                try:
                    await async_job_svc.mark_failed(db, job_uuid, error=err)
                    await notify_job_terminal(db, job_id=job_uuid)
                    await db.commit()
                except Exception:
                    await db.rollback()
                raise
    finally:
        await engine.dispose()
