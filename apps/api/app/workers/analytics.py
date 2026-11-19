"""DuckDB 离线分析同步 Celery 任务（ROADMAP §1.6）。

每日 02:30 UTC 增量拉 task_events + audit_logs 到 DuckDB 文件，
供 /admin/analytics 端点出 3 个固定面板。
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(name="app.workers.analytics.sync_to_duckdb")
def sync_to_duckdb() -> dict:
    return asyncio.run(_sync_async())


async def _sync_async() -> dict:
    """同步入口。每次任务**独立** engine + session，避免 Celery prefork
    多 worker 共享全局连接池触发 asyncpg "another operation in progress"。
    """
    from app.services.duckdb_sync import sync_all

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with SessionLocal() as db:
            result = await sync_all(db)
    finally:
        await engine.dispose()
    log.info("sync_to_duckdb: %s", result)
    return result
