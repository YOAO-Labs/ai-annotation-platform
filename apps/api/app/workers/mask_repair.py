from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.mask_repair_execution import (
    run_mask_repair as execute_mask_repair,
    run_mask_repair_rollback as execute_mask_repair_rollback,
)
from app.config import settings
from app.services.gpu_arbitration.dispatch import build_gpu_dispatch_context_factory
from app.services.jobs_queue import build_video_tracker_job_dispatcher
from app.workers.celery_app import celery_app


async def _run_mask_repair(
    batch_id: str,
    celery_task_id: str | None,
    *,
    tracker_dispatcher,
) -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    dispatch_context_factory = build_gpu_dispatch_context_factory(session_factory)
    try:
        await execute_mask_repair(
            batch_id,
            celery_task_id,
            session_factory=session_factory,
            tracker_dispatcher=tracker_dispatcher,
            dispatch_context_factory=dispatch_context_factory,
        )
    finally:
        await engine.dispose()


async def _rollback_mask_repair(
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
        await execute_mask_repair_rollback(
            batch_id,
            celery_task_id,
            session_factory=session_factory,
        )
    finally:
        await engine.dispose()


@celery_app.task(bind=True, name="app.workers.mask_repair.run_mask_repair")
def run_mask_repair(self, batch_id: str) -> None:
    tracker_dispatcher = build_video_tracker_job_dispatcher()
    asyncio.run(
        _run_mask_repair(
            batch_id,
            getattr(getattr(self, "request", None), "id", None),
            tracker_dispatcher=tracker_dispatcher,
        )
    )


@celery_app.task(bind=True, name="app.workers.mask_repair.rollback_mask_repair")
def rollback_mask_repair(self, batch_id: str) -> None:
    asyncio.run(
        _rollback_mask_repair(
            batch_id,
            getattr(getattr(self, "request", None), "id", None),
        )
    )
