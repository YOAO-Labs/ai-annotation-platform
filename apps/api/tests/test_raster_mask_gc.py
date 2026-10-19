from datetime import datetime, timedelta, timezone

import pytest

from app.application.raster_mask_retention import (
    is_raster_mask_key_referenced,
    referenced_raster_mask_keys,
)
from app.db.models.mask_review_scope import MaskReviewScope
from app.db.models.video_tracker_job import VideoTrackerJob, VideoTrackerJobStatus
from app.services.raster_mask_storage import _eligible_raster_mask_objects
from app.services.video_tracking.jobs import expire_stale_tracker_candidates
from tests.test_video_tracker_jobs_list import _make_video_task
from tests.test_mask_repair_m5 import _seed_repair_issue
from tests.test_video_tracker_input_native import _seed_tracker_job, _capture, _reply


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


@pytest.mark.parametrize("decision", ["accept", "reject"])
async def test_gc_preserves_review_region_after_its_qc_issue_is_deleted(
    db_session,
    super_admin,
    decision,
):
    user, _ = super_admin
    project, task, annotation, issue, _, _ = await _seed_repair_issue(
        db_session,
        owner_id=user.id,
    )
    reference = issue.region_mask_ref
    key = reference["object_key"]
    scope = MaskReviewScope(
        project_id=project.id,
        task_id=task.id,
        annotation_id=annotation.id,
        qc_issue_id=issue.id,
        reviewer_id=user.id,
        source_annotation_version=1,
        result_annotation_version=1,
        source_job_revision=1,
        frame_start=0,
        frame_end=0,
        region_mask_ref=reference,
        region_digest=reference["sha256"],
        candidate_digest="sha256:" + "a" * 64,
        decision=decision,
    )
    db_session.add(scope)
    await db_session.flush()
    await db_session.delete(issue)
    await db_session.flush()
    await db_session.refresh(scope)
    assert scope.qc_issue_id is None
    assert key in await referenced_raster_mask_keys(db_session)
    assert await is_raster_mask_key_referenced(db_session, key)
    await db_session.delete(scope)
    await db_session.flush()
    assert key not in await referenced_raster_mask_keys(db_session)
    assert not await is_raster_mask_key_referenced(db_session, key)


@pytest.mark.parametrize(
    "status,expired,retained",
    [
        ("queued", False, True),
        ("running", False, True),
        ("pending_review", False, True),
        ("partially_reviewed", False, True),
        ("cancelled", False, True),
        ("pending_review", True, False),
        ("completed", False, False),
    ],
)
async def test_gc_keeps_mask_seeds_in_retained_native_tracker_scope(
    db_session,
    status,
    expired,
    retained,
):
    from app.services.raster_mask_storage import build_rle_reference

    reference = build_rle_reference(
        {
            "encoding": "coco_rle",
            "size": [360, 640],
            "counts": [1, 230399],
        }
    )
    key = reference["object_key"]
    actor, _, _, _, job = await _seed_tracker_job(
        db_session,
        prompt={
            "text": "person",
            "seeds": [
                {
                    "obj_id": 1,
                    "geometry": {"type": "mask", "mask": reference},
                }
            ],
        },
    )
    captured = _reply(await _capture(db_session, actor, job.id))
    assert captured["ok"] is True
    # Only the accepted scope carries this key; current job facts no longer do.
    job.prompt = {"text": "new prompt"}
    job.status = status
    job.staged_result = {"results": [{"geometry": {"type": "bbox"}}]}
    job.completed_at = datetime.now(timezone.utc) - timedelta(
        hours=25 if expired else 0
    )
    await db_session.flush()
    assert (key in await referenced_raster_mask_keys(db_session)) is retained
    assert await is_raster_mask_key_referenced(db_session, key) is retained
