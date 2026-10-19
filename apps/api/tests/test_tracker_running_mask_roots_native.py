"""Running tracker roots outlive producer grace and transfer on final staging."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.video_tracker_job import VideoTrackerJob
from app.services.mask_gc import (
    begin_mask_content_write,
    register_tracker_running_mask_root,
)
from tests.test_batch_result_consumers_native import _world
from tests.test_batch_result_consumers_native import pytestmark as pytestmark
from tests.test_mask_gc_native import _key, _stage_one_old_candidate
from tests.test_video_tracker_jobs_list import _make_job, _make_video_task


async def test_running_masks_survive_expired_grace_and_transfer_to_staged_roots(
    test_engine,
):
    async with _world(test_engine, staged=False) as world:
        key = _key()
        async with world.owner_factory() as db:
            task, item = await _make_video_task(db, world.actor_id)
            job = await _make_job(db, task, item, world.actor_id, status="running")
            job_id, task_id = job.id, task.id
            await db.commit()
        async with world.writers() as db:
            await begin_mask_content_write(db, [key])
            assert await register_tracker_running_mask_root(
                db, job_id=job_id, task_id=task_id, object_key=key
            )
            await db.commit()
        async with world.owner_factory() as db:
            await db.execute(
                text(
                    "UPDATE raster_mask_uploads SET created_at=:past WHERE object_key=:key"
                ),
                {"key": key, "past": datetime.now(timezone.utc) - timedelta(days=3)},
            )
            await db.execute(
                text(
                    "UPDATE mask_gc_key_epochs SET last_write_at=:past WHERE object_key=:key"
                ),
                {"key": key, "past": datetime.now(timezone.utc) - timedelta(days=3)},
            )
            await db.commit()
            await _stage_one_old_candidate(db, key, expected_staged=0)
            roots = (
                (
                    await db.execute(
                        text(
                            "SELECT root_kind FROM mask_gc_roots WHERE object_key=:key"
                        ),
                        {"key": key},
                    )
                )
                .scalars()
                .all()
            )
            assert roots == ["video_tracker_running_outputs"]
            await begin_mask_content_write(db, [key])
            job = await db.get(VideoTrackerJob, job_id)
            job.status = "pending_review"
            job.staged_result = {"tracks": [{"mask": {"object_key": key}}]}
            await db.commit()
            roots = (
                (
                    await db.execute(
                        text(
                            "SELECT root_kind FROM mask_gc_roots WHERE object_key=:key"
                        ),
                        {"key": key},
                    )
                )
                .scalars()
                .all()
            )
            assert "video_tracker_running_outputs" not in roots
            assert "video_tracker_jobs" in roots
            await _stage_one_old_candidate(db, key, expected_staged=0)
            job.status = "discarded"
            job.staged_result = {}
            await db.commit()
            await db.execute(
                text(
                    "UPDATE raster_mask_uploads SET created_at=:past WHERE object_key=:key"
                ),
                {"key": key, "past": datetime.now(timezone.utc) - timedelta(days=3)},
            )
            await db.execute(
                text(
                    "UPDATE mask_gc_key_epochs SET last_write_at=:past WHERE object_key=:key"
                ),
                {"key": key, "past": datetime.now(timezone.utc) - timedelta(days=3)},
            )
            await _stage_one_old_candidate(db, key, expected_staged=1)
            await db.commit()


async def test_root_registration_refuses_changed_owner_and_cleanup_login(test_engine):
    async with _world(test_engine, staged=False) as world:
        async with world.owner_factory() as db:
            task, item = await _make_video_task(db, world.actor_id)
            job = await _make_job(db, task, item, world.actor_id, status="running")
            task_id, job_id = task.id, job.id
            await db.commit()
        key = _key()
        async with world.writers() as db:
            await begin_mask_content_write(db, [key])
            assert not await register_tracker_running_mask_root(
                db, job_id=job_id, task_id=uuid.uuid4(), object_key=key
            )
            await db.rollback()
        async with world.owner_factory() as db:
            await db.execute(
                text(
                    "UPDATE video_tracker_jobs SET cancel_requested_at=clock_timestamp() WHERE id=:id"
                ),
                {"id": job_id},
            )
            await db.commit()
        async with world.writers() as db:
            await begin_mask_content_write(db, [key])
            assert not await register_tracker_running_mask_root(
                db, job_id=job_id, task_id=task_id, object_key=key
            )
            await db.rollback()
        engine = create_async_engine(os.environ["AAP_MODULE_DATA_LABELING_MASK_GC_URL"])
        try:
            async with async_sessionmaker(engine)() as db:
                with pytest.raises(DBAPIError) as error:
                    await db.execute(
                        text(
                            "SELECT aap_labeling_v1.register_tracker_running_mask_root(CAST(:payload AS jsonb))"
                        ),
                        {
                            "payload": json.dumps(
                                {
                                    "job_id": str(job_id),
                                    "task_id": str(task_id),
                                    "object_key": key,
                                }
                            )
                        },
                    )
                assert getattr(error.value.orig, "sqlstate", None) == "42501"
                await db.rollback()
        finally:
            await engine.dispose()


async def test_job_lock_contention_returns_wait_without_minting_an_unrooted_claim(
    test_engine,
):
    from app.services.raster_mask_contract import RasterMaskContractError

    async with _world(test_engine, staged=False) as world:
        async with world.owner_factory() as db:
            task, item = await _make_video_task(db, world.actor_id)
            job = await _make_job(db, task, item, world.actor_id, status="running")
            task_id, job_id = task.id, job.id
            await db.commit()
        async with world.owner_factory() as locker:
            await locker.execute(
                text("SELECT id FROM video_tracker_jobs WHERE id=:id FOR UPDATE"),
                {"id": job_id},
            )
            async with world.writers() as db:
                key = _key()
                await begin_mask_content_write(db, [key])
                with pytest.raises(RasterMaskContractError) as error:
                    await register_tracker_running_mask_root(
                        db, job_id=job_id, task_id=task_id, object_key=key
                    )
                assert error.value.detail["reason"] == "mask_storage_unavailable"
                await db.rollback()
            await locker.rollback()
        async with world.owner_factory() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM mask_gc_roots WHERE root_kind='video_tracker_running_outputs' AND root_id=:id"
                    ),
                    {"id": str(job_id)},
                )
                == 0
            )
