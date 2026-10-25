"""Celery 任务专用 DB 会话。

Celery prefork worker 每个任务用 ``asyncio.run()`` 跑一个**新事件循环**。若复用
``app.db.base`` 的全局 engine，其 asyncpg 连接会绑死在某个已关闭的事件循环上：
任务结束 loop 关闭后，池里这条带着 ``BEGIN`` 的连接既无法被下个 loop 复用，也来不及
``ROLLBACK``，在 PostgreSQL 侧残留为 ``idle in transaction`` 长期泄漏，最终拖垮连接数。

因此每个任务用**独立 engine**，并在结束时 ``dispose()`` 干净关闭其全部连接
（与 ``analytics.py`` / ``export.py`` 已有的内联写法一致，此处抽成共享帮助器）。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings


@asynccontextmanager
async def task_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        settings.database_url, echo=False, hide_parameters=True
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@asynccontextmanager
async def task_session() -> AsyncIterator[AsyncSession]:
    async with task_session_factory() as factory:
        async with factory() as session:
            yield session
