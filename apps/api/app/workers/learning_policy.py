"""Periodic reconciliation uses durable identities; overlapping ticks are safe."""

import asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from app.config import settings
from app.application.learning_policy_execution import tick
from app.services.training_datasets import bind_training_authority
from app.workers.celery_app import celery_app
from app.workers.training_dataset_dispatch import CeleryTrainingDatasetDispatcher


async def _tick():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        await tick(
            async_sessionmaker(engine, expire_on_commit=False),
            dispatcher=CeleryTrainingDatasetDispatcher(),
            training_authority_factory=bind_training_authority,
        )
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.learning_policy.check_policies")
def check_policies():
    asyncio.run(_tick())
