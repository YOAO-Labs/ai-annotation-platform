from datetime import datetime, timedelta, timezone

import pytest

from app.application.raster_mask_retention import (
    referenced_raster_mask_keys,
)
from app.db.models.video_tracker_job import VideoTrackerJob, VideoTrackerJobStatus
from app.services.raster_mask_storage import _eligible_raster_mask_objects
from app.services.video_tracking.jobs import expire_stale_tracker_candidates
from tests.test_video_tracker_jobs_list import _make_video_task


def test_raster_mask_gc_keeps_referenced_and_grace_period_objects():
    now = datetime.now(timezone.utc)
    candidates = [
        {
            "key": "raster-masks/sha256/aa/aa/referenced.json",
            "last_modified": now - timedelta(days=3),
        },
        {
            "key": "raster-masks/sha256/bb/bb/recent.json",
            "last_modified": now - timedelta(hours=2),
        },
        {
            "key": "raster-masks/sha256/cc/cc/orphan.json",
            "last_modified": now - timedelta(days=2),
        },
        {
            "key": "raster-masks/sha256/dd/dd/exact-cutoff.json",
            "last_modified": now - timedelta(hours=24),
        },
        {
            "key": "raster-masks/sha256/ee/ee/unknown-age.json",
            "last_modified": None,
        },
    ]
    eligible = _eligible_raster_mask_objects(
        candidates,
        {"raster-masks/sha256/aa/aa/referenced.json"},
        now - timedelta(hours=24),
    )
    assert [item["key"] for item in eligible] == [
        "raster-masks/sha256/cc/cc/orphan.json"
    ]


def test_raster_mask_gc_caps_each_run_at_1000():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    old = cutoff - timedelta(days=1)
    candidates = [
        {"key": f"raster-masks/sha256/{index:04d}.json", "last_modified": old}
        for index in range(1005)
    ]
    assert len(_eligible_raster_mask_objects(candidates, set(), cutoff)) == 1000


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
    assert key in await referenced_raster_mask_keys(db_session)


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

    expired = await expire_stale_tracker_candidates(db_session)
    await db_session.refresh(job)

    assert expired == 1
    assert job.status == VideoTrackerJobStatus.DISCARDED.value
    assert job.staged_result is None
    assert key not in await referenced_raster_mask_keys(db_session)
