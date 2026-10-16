"""Celery producer adapter for training dataset jobs."""

from __future__ import annotations

import asyncio
from uuid import UUID


class CeleryTrainingDatasetDispatcher:
    __slots__ = ()

    async def dispatch_dataset(
        self,
        version_id: UUID,
        job_id: UUID,
        attempt_id: UUID,
        *,
        execution_id: str,
    ) -> None:
        from app.workers.training_dataset import run_training_dataset

        await asyncio.to_thread(
            run_training_dataset.apply_async,
            args=[str(version_id), str(job_id), str(attempt_id)],
            task_id=execution_id,
        )
