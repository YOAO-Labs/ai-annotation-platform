from __future__ import annotations

import asyncio


from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.mask_format_import import (
    run_mask_format_import as execute_mask_format_import,
)
from app.config import settings
from app.workers.celery_app import celery_app


async def _run_mask_format_import(
    batch_id: str,
    celery_task_id: str | None,
) -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        await execute_mask_format_import(
            batch_id,
            celery_task_id,
            session_factory=session_factory,
        )
    finally:
        await engine.dispose()


@celery_app.task(
    bind=True, name="app.workers.mask_format_import.run_mask_format_import"
)
def run_mask_format_import(self, batch_id: str) -> None:
    asyncio.run(
        _run_mask_format_import(
            batch_id,
            getattr(getattr(self, "request", None), "id", None),
        )
    )
