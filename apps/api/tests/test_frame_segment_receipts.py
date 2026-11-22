"""Frame upgrade acceptance against the worktree's isolated Redis and PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
import redis

from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.notification import Notification
from app.workers import frame_preannotate
from sqlalchemy import select
from sqlalchemy.engine import make_url

from tests.test_frame_preannotate_persistence import frame_world as frame_world

pytestmark = pytest.mark.skipif(
    os.environ.get("AAP_FRAME_CELERY_ACCEPTANCE") != "1"
    or os.environ.get("YOAO_WORKTREE_MODE") != "test",
    reason="Requires the owned Redis and PostgreSQL used by frame acceptance",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_upgrade_recovers_only_known_legacy_frame_receipts(
    frame_world, test_db_url, monkeypatch, expired
):
    world = frame_world
    redis_url = make_url(os.environ["REDIS_URL"])
    assert redis_url.host == "127.0.0.1"
    assert make_url(settings.redis_url) == redis_url
    monkeypatch.setattr(settings, "database_url", test_db_url)
    job_id, project_id = str(world.job_id), str(world.project_id)
    async with world.maker() as db:
        job = await db.get(AsyncJob, world.job_id)
        job.result = {
            **job.result,
            "frame_dispatch": {
                "segment_ids": ["first", "second", "last"],
                "total_frames": 3,
            },
        }
        job.progress_pct = 33
        await db.commit()

    stats = {"task_id": str(world.task_id), "frames_done": 1, "boxes": 2}
    key = frame_preannotate._segment_results_key(job_id)
    done = frame_preannotate._progress_key(job_id)
    total = frame_preannotate._progress_total_key(job_id)
    with redis.from_url(settings.redis_url) as progress:
        try:
            progress.hset(key, "first", json.dumps({"version": 1, "stats": stats}))
            progress.set(done, 1)
            progress.set(total, 3)
            if expired:
                for cache_key in (key, done, total):
                    assert progress.pexpire(cache_key, 200)
                async with asyncio.timeout(5):
                    while progress.exists(key, done, total):
                        await asyncio.sleep(0.02)

            async def settle(segment_id, values=stats):
                return await frame_preannotate._write_job_progress(
                    job_id, segment_id, project_id, segment_stats=values
                )

            with pytest.raises(ValueError, match="frozen frame dispatch"):
                await settle("wrong")
            await settle("second")
            progress.delete(key, done, total)

            if not expired:
                await frame_preannotate._finalize(project_id=project_id, job_id=job_id)
                async with world.maker() as db:
                    job = await db.get(AsyncJob, world.job_id)
                    assert job.status == "running"
                    assert job.progress_pct == 66
                    assert set(job.payload["frame_segment_receipts"]) == {
                        "first",
                        "second",
                    }
                assert (await settle("first", {"skipped": 1}))["boxes"] == 2

            await settle("last")
            await frame_preannotate._finalize(project_id=project_id, job_id=job_id)
            async with world.maker() as db:
                job = await db.get(AsyncJob, world.job_id)
                if expired:
                    assert job.status == "failed"
                    assert job.error_message == "frame_segment_receipts_unavailable"
                    assert "frames_done" not in job.result
                    notifications = (
                        await db.scalars(
                            select(Notification.type).where(
                                Notification.target_id == world.job_id
                            )
                        )
                    ).all()
                    assert notifications == ["job.failed"]
                else:
                    assert job.status == "completed"
                    assert job.result["frames_done"] == 3
                    assert job.result["boxes"] == 6
                    assert job.result["skipped"] == 0
        finally:
            progress.delete(key, done, total)
