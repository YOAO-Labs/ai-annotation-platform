"""v0.12.0 · B4 建任务异步化 worker。

大 dataset link（item 数 > TASK_CREATE_SYNC_THRESHOLD）的建 task 从同步 HTTP
单事务搬到 Celery：分块 INSERT + 每块 commit + 进度上报，避免超时与长事务锁。
核心分块逻辑在 app.services.dataset.build_tasks_for_link（worker 与同步快路径共用）。
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
from app.services.dataset import build_tasks_for_link
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

                # build_tasks_for_link 内部分块 commit + 上报进度；这里只需收尾。
                result = await build_tasks_for_link(
                    db,
                    dataset_id=dataset_id,
                    project_id=project_id,
                    job_id=job_uuid,
                    directory_authority=payload.get("directory_authority"),
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
