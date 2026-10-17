from __future__ import annotations

import asyncio
import uuid

from app.application.quality_execution import execute_video_track_quality
from app.workers._db import task_session
from app.workers.celery_app import celery_app


@celery_app.task(bind=True, name="app.workers.video_track_quality.run")
def run_video_track_quality(self, run_id: str) -> None:
    asyncio.run(_run(uuid.UUID(run_id), getattr(self.request, "id", None)))


async def _run(run_id: uuid.UUID, celery_task_id: str | None) -> None:
    async with task_session() as db:
        await execute_video_track_quality(
            db,
            run_id=run_id,
            celery_task_id=celery_task_id,
        )
