"""Business liveness independent of optional QC jobs and upload quota owners."""

from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import delete

from app.db.models.annotation import Annotation
from app.db.models.mask_review_scope import MaskReviewScope
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.db.models.task import Task
from app.services.raster_mask_references import is_referenced, referenced_keys
from tests.test_video_tracker_jobs_list import _make_video_task


async def test_current_review_scope_survives_missing_job_and_issue(
    db_session, super_admin
):
    user, _ = super_admin
    task, _ = await _make_video_task(db_session, user.id)
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 1, "y": 2, "w": 10, "h": 12},
        version=2,
    )
    db_session.add(annotation)
    await db_session.flush()
    key = f"raster-masks/sha256/{uuid.uuid4().hex}.json"
    scope = MaskReviewScope(
        project_id=task.project_id,
        task_id=task.id,
        annotation_id=annotation.id,
        qc_issue_id=None,
        source_job_id=None,
        reviewer_id=user.id,
        source_annotation_version=1,
        result_annotation_version=2,
        source_job_revision=1,
        frame_start=0,
        frame_end=1,
        region_mask_ref={"object_key": key},
        region_digest="a" * 64,
        candidate_digest="sha256:" + "b" * 64,
        decision="accept",
    )
    db_session.add(scope)
    await db_session.flush()
    assert key in await referenced_keys(db_session)
    assert await is_referenced(db_session, key)
    annotation.version = 3
    await db_session.flush()
    assert key not in await referenced_keys(db_session)
    assert not await is_referenced(db_session, key)
    annotation.version = 2
    annotation.is_active = False
    await db_session.flush()
    assert not await is_referenced(db_session, key)


async def test_only_fresh_unlinked_upload_reservations_retain_content(
    db_session, super_admin
):
    user, _ = super_admin
    task, _ = await _make_video_task(db_session, user.id)
    now = datetime.now(timezone.utc)
    keys = {
        name: f"raster-masks/sha256/{uuid.uuid4().hex}.json"
        for name in ("fresh", "expired", "linked")
    }
    for name, key in keys.items():
        db_session.add(
            RasterMaskUpload(
                task_id=task.id,
                object_key=key,
                created_at=now - timedelta(hours=25 if name == "expired" else 1),
                linked_at=now if name == "linked" else None,
            )
        )
    await db_session.flush()
    retained = await referenced_keys(db_session)
    for name, key in keys.items():
        assert (key in retained) is (name == "fresh")
        assert await is_referenced(db_session, key) is (name == "fresh")
    await db_session.execute(delete(Task).where(Task.id == task.id))
    assert not await is_referenced(db_session, keys["fresh"])
