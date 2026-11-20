"""predictions 月分区维护 Celery 任务（ADR-0006）。

ensure_future_prediction_partitions: 每月 25 日 03:30 UTC，提前建好未来分区。
"""

from __future__ import annotations

import asyncio
import logging

from app.services.prediction_partition_service import PredictionPartitionService
from app.workers._db import task_session
from app.workers.celery_app import celery_app


log = logging.getLogger(__name__)


@celery_app.task(
    name="app.workers.prediction_partition.ensure_future_prediction_partitions"
)
def ensure_future_prediction_partitions(months_ahead: int = 3) -> dict:
    return asyncio.run(_ensure_async(months_ahead))


async def _ensure_async(months_ahead: int) -> dict:
    async with task_session() as db:
        created = await PredictionPartitionService.ensure_future_partitions(
            db, months_ahead=months_ahead
        )
        await db.commit()
    log.info(
        "ensure_future_prediction_partitions: created=%d %s", len(created), created
    )
    return {"created": created}
