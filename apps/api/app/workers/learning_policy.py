"""Periodic reconciliation uses durable identities; overlapping ticks are safe."""

import asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from app.config import settings
from app.services.learning_policies import tick
from app.workers.celery_app import celery_app


async def _tick():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        await tick(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.learning_policy.check_policies")
def check_policies():
    asyncio.run(_tick())
