"""v0.10.16 · async_jobs 终态 retention purge。

每日 04:15 UTC 跑一次，删除 30 天前的 completed/failed/cancelled 行。
running/pending 行**永不**被清；如果出现 running 卡死的情况由信号兜底翻 failed。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.services import async_job as async_job_svc
from app.workers._db import task_session
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)

RETENTION_DAYS = 30


@celery_app.task(name="app.workers.async_jobs_cleanup.purge_old_async_jobs")
def purge_old_async_jobs() -> dict:
    return asyncio.run(_purge_async())


async def _purge_async() -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    async with task_session() as db:
        ids = await async_job_svc.delete_terminal_jobs_before(
            db,
            cutoff,
            statuses=("completed", "failed", "cancelled"),
        )
        await db.commit()
    log.info("purge_old_async_jobs: removed=%d cutoff=%s", len(ids), cutoff.isoformat())
    return {"removed": len(ids), "cutoff": cutoff.isoformat()}
