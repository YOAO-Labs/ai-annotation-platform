from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models.video_tracker_job import VideoTrackerJob, VideoTrackerJobStatus
from app.workers.cleanup import (
    _expire_mask_annotation_revisions,
    _expire_stale_video_tracker_candidates,
    _referenced_raster_mask_keys,
)
from tests.test_video_tracker_jobs_list import _make_video_task


@pytest.mark.asyncio
async def test_expire_mask_annotation_revisions_uses_database_retention_deadline():
    result = MagicMock()
    result.scalars.return_value.all.return_value = ["revision-1", "revision-2"]
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    now = datetime.now(timezone.utc)

    assert await _expire_mask_annotation_revisions(db, now=now) == 2
    statement = db.execute.await_args.args[0]
    assert "mask_annotation_revisions.expires_at" in str(statement)


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_worker_expires_business_ttls_only_before_live_owner_collection(
    monkeypatch, dry_run
):
    import app.workers.cleanup as cleanup
    from app.services import raster_mask_objects

    events = []
    db = SimpleNamespace(
        execute=AsyncMock(),
        commit=AsyncMock(side_effect=lambda: events.append("commit")),
    )

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(cleanup, "task_session", session)
    expire_tracker = AsyncMock(return_value=2)
    expire_conversion = AsyncMock(return_value=3)
    expire_revisions = AsyncMock(return_value=4)
    refresh = AsyncMock()
    monkeypatch.setattr(
        cleanup, "_expire_stale_video_tracker_candidates", expire_tracker
    )
    monkeypatch.setattr(
        cleanup, "_expire_annotation_conversion_plans", expire_conversion
    )
    monkeypatch.setattr(cleanup, "_expire_mask_annotation_revisions", expire_revisions)
    monkeypatch.setattr(
        cleanup, "refresh_raster_mask_active_geometries_safely", refresh
    )
    collect = AsyncMock(
        side_effect=lambda *args, **kwargs: events.append("collect") or {"deleted": 7}
    )
    monkeypatch.setattr(raster_mask_objects, "cleanup", collect)

    result = await cleanup._purge_unreferenced_raster_masks_async(dry_run=dry_run)
    collect.assert_awaited_once_with(db, dry_run=dry_run, limit=1000)
    assert result == {
        "deleted": 7,
        "expired_tracker_candidates": 0 if dry_run else 2,
        "expired_conversion_plans": 0 if dry_run else 3,
        "expired_mask_revisions": 0 if dry_run else 4,
    }
    if dry_run:
        db.execute.assert_not_awaited()
        db.commit.assert_not_awaited()
        for operation in (expire_tracker, expire_conversion, expire_revisions, refresh):
            operation.assert_not_awaited()
        assert events == ["collect"]
    else:
        assert events == ["commit", "collect"]
        for operation in (expire_tracker, expire_conversion, expire_revisions, refresh):
            operation.assert_awaited_once_with(db)


@pytest.mark.asyncio
async def test_gc_keeps_partially_reviewed_tracker_mask_reference(
    db_session, super_admin
):
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    key = "raster-masks/sha256/aa/aa/partial.json"
    db_session.add(
        VideoTrackerJob(
            task_id=task.id,
            dataset_item_id=item.id,
            annotation_id=None,
            created_by=user.id,
            status=VideoTrackerJobStatus.PARTIALLY_REVIEWED.value,
            model_key="sam3_video",
            direction="forward",
            from_frame=0,
            to_frame=1,
            prompt={},
            staged_result={
                "results": [
                    {
                        "frame_index": 1,
                        "geometry": {
                            "type": "mask",
                            "mask": {"object_key": key},
                        },
                    }
                ]
            },
            event_channel="video-tracker-job:test",
        )
    )
    await db_session.flush()
    assert key in await _referenced_raster_mask_keys(db_session)


@pytest.mark.asyncio
async def test_gc_expires_abandoned_staged_candidates_and_releases_refs(
    db_session, super_admin
):
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    key = "raster-masks/sha256/aa/aa/expired.json"
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=None,
        created_by=user.id,
        status=VideoTrackerJobStatus.PENDING_REVIEW.value,
        model_key="sam3_video",
        direction="forward",
        from_frame=0,
        to_frame=1,
        prompt={},
        staged_result={
            "results": [
                {
                    "frame_index": 1,
                    "geometry": {"type": "mask", "mask": {"object_key": key}},
                }
            ]
        },
        completed_at=datetime.now(timezone.utc) - timedelta(hours=25),
        event_channel="video-tracker-job:expired",
    )
    db_session.add(job)
    await db_session.flush()

    expired = await _expire_stale_video_tracker_candidates(db_session)
    await db_session.refresh(job)

    assert expired == 1
    assert job.status == VideoTrackerJobStatus.DISCARDED.value
    assert job.staged_result is None
    assert key not in await _referenced_raster_mask_keys(db_session)
