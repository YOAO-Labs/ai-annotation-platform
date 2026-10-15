"""Periodic finite-study reconciliation; ordinary executors poll admitted runs."""

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.services.training_studies import tick
from app.workers.celery_app import celery_app


async def _tick():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        await tick(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.training_study.check_studies")
def check_studies():
    asyncio.run(_tick())
