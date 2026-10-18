"""Celery adapter for the Assets-owned committed storage cleanup queue."""

import asyncio
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.services.asset_storage_cleanup import drain_asset_storage_deletions
from app.services.storage import StorageService
from app.workers.celery_app import celery_app


async def _run_asset_storage_cleanup(
    operation_id: str | None, limit: int
) -> dict[str, int]:
    engine = create_async_engine(settings.database_url, hide_parameters=True)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as db:
            return await drain_asset_storage_deletions(
                db,
                StorageService(),
                operation_id=UUID(operation_id) if operation_id is not None else None,
                limit=limit,
            )
    finally:
        await engine.dispose()


@celery_app.task(
    name="app.workers.asset_storage_cleanup.retry_asset_storage_deletions",
    acks_late=True,
    reject_on_worker_lost=True,
)
def retry_asset_storage_deletions(
    operation_id: str | None = None, limit: int = 100
) -> dict[str, int]:
    return asyncio.run(_run_asset_storage_cleanup(operation_id, limit))
