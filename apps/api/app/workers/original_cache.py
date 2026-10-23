"""Recover cache reservations and retire only owned, rebuildable generations."""

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services.original_cache import maintain
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.original_cache.maintain_original_cache")
def maintain_original_cache():
    return asyncio.run(_maintain())


async def _maintain():
    engine = create_async_engine(settings.database_url, echo=False)
    try:
        return await maintain(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()
