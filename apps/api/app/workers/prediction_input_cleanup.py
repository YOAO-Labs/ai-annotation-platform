"""Recover only expired or sealed prediction input attempts."""

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services.prediction_inputs import maintain
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.prediction_input_cleanup.recover_prediction_inputs")
def recover_prediction_inputs():
    return asyncio.run(_recover())


async def _recover():
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await maintain(factory)
    finally:
        await engine.dispose()
