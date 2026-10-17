"""Celery adapter for Raster Mask QC execution."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.quality_execution import execute_mask_qc
from app.config import settings
from app.workers.celery_app import celery_app


@celery_app.task(bind=True, name="app.workers.mask_qc.run_mask_qc")
def run_mask_qc(self, run_id: str) -> None:
    asyncio.run(
        _run_mask_qc(
            run_id=run_id,
            celery_task_id=getattr(self.request, "id", None),
        )
    )


async def _run_mask_qc(*, run_id: str, celery_task_id: str | None) -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with session_factory() as db:
            await execute_mask_qc(
                db,
                run_id=uuid.UUID(run_id),
                celery_task_id=celery_task_id,
            )
    finally:
        await engine.dispose()
