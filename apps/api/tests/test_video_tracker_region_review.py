from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from app.db.models.annotation import Annotation
from app.db.models.dataset import VideoSegment
from app.db.models.mask_qc import MaskQCIssue
from app.db.models.mask_review_scope import MaskReviewScope
from app.db.models.task_batch import TaskBatch
from app.db.models.video_tracker_job import VideoTrackerJob, VideoTrackerJobStatus
from app.services.raster_mask_storage import build_rle_reference
from app.schemas.video_tracker_job import VideoTrackerDecisionRequest
from app.services.video_tracking import runner
from app.utils.raster_mask_rle import decode_coco_rle, encode_coco_rle
from tests.test_video_tracker_jobs_list import _bearer, _make_video_task
from tests.factory import create_membership


def test_decision_selectors_are_mutually_exclusive() -> None:
    common = {
        "decision": "accept",
        "expected_source_versions": {},
        "job_revision": 1,
    }
    region = VideoTrackerDecisionRequest.model_validate(
        {
            **common,
            "qc_issue_id": str(uuid.uuid4()),
            "candidate_digest": f"sha256:{'a' * 64}",
        }
    )
    assert region.instance_ids is None
    with pytest.raises(ValidationError):
        VideoTrackerDecisionRequest.model_validate(
            {
                **common,
                "qc_issue_id": str(uuid.uuid4()),
                "candidate_digest": f"sha256:{'a' * 64}",
                "instance_ids": ["1"],
                "from_frame": 1,
                "to_frame": 1,
            }
        )


def _rle(rows: list[str]) -> dict:
    height = len(rows)
    width = len(rows[0])
    return encode_coco_rle(
        [1 if value == "#" else 0 for row in rows for value in row], width, height
    )


def _rows(rle: dict) -> list[str]:
    height, width = rle["size"]
    pixels = decode_coco_rle(rle)
    return [
        "".join("#" if pixels[y * width + x] else "." for x in range(width))
        for y in range(height)
    ]


def _patch_mask_storage(monkeypatch, *rles: dict) -> dict[str, dict]:
    objects = {
        build_rle_reference(item)["sha256"]: copy.deepcopy(item) for item in rles
    }

    async def load(reference: dict) -> dict:
        return copy.deepcopy(objects[str(reference["sha256"])])

    async def store(rle: dict) -> dict:
        reference = build_rle_reference(rle)
        objects[reference["sha256"]] = copy.deepcopy(rle)
        return reference

    async def lock(*_args, **_kwargs) -> list[str]:
        return []

    monkeypatch.setattr(runner, "load_coco_rle", load)
    monkeypatch.setattr(runner, "store_coco_rle", store)
    monkeypatch.setattr(runner, "lock_raster_mask_references", lock)
    return objects


async def _seed_region_review(
    db,
    *,
    owner_id,
    current: dict,
    candidate: dict,
    region: dict,
    source: str = "prediction",
):
    task, item = await _make_video_task(db, owner_id)
    item.width = 4
    item.height = 4
    current_ref = build_rle_reference(current)
    candidate_ref = build_rle_reference(candidate)
    region_ref = build_rle_reference(region)
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=owner_id,
        annotation_type="video_track_mask",
        class_name="car",
        tool_unit_id="region",
        geometry={
            "type": "video_track_mask",
            "track_id": "track-mask",
            "keyframes": [
                {
                    "frame_index": 1,
                    "mask": current_ref,
                    "source": source,
                    "occluded": False,
                }
            ],
            "outside": [],
        },
        track_id="track-mask",
    )
    db.add(annotation)
    await db.flush()
    candidate_row = runner._ensure_candidate_contract(
        {
            "frame_index": 1,
            "geometry": {
                "type": "mask",
                "mask": candidate_ref,
                "bbox": {"x": 0.25, "y": 0.25, "w": 0.75, "h": 0.5},
            },
            "confidence": 0.9,
            "outside": False,
            "instance_id": "1",
            "primary": True,
        }
    )
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=owner_id,
        status=VideoTrackerJobStatus.PENDING_REVIEW.value,
        model_key="sam2_video",
        direction="forward",
        from_frame=1,
        to_frame=1,
        prompt={"expected_source_versions": {str(annotation.id): 1}},
        staged_result={
            "results": [candidate_row],
            "grid_step": 1,
            "output_geometry": "mask",
            "source_instance_id": "1",
        },
        event_channel="video-tracker-job:test-region",
    )
    issue = MaskQCIssue(
        project_id=task.project_id,
        task_id=task.id,
        annotation_id=annotation.id,
        annotation_version=1,
        related_annotation_ids=[annotation.id],
        source_versions={str(annotation.id): 1},
        code="boundary_noise",
        severity="warning",
        severity_rank=1,
        status="open",
        frame_start=1,
        frame_end=1,
        metric={},
        threshold={},
        region_mask_ref=region_ref,
        region_digest=region_ref["sha256"],
        dedupe_key=uuid.uuid4().hex.ljust(64, "0"),
        source={},
    )
    db.add_all([job, issue])
    await db.commit()
    return task, item, annotation, job, issue, candidate_row


def _decision(
    annotation: Annotation, job: VideoTrackerJob, issue: MaskQCIssue, digest: str
):
    return {
        "qc_issue_id": str(issue.id),
        "candidate_digest": digest,
        "decision": "accept",
        "expected_source_versions": {str(annotation.id): int(annotation.version)},
        "job_revision": int(job.revision),
    }


async def test_region_accept_changes_only_issue_pixels_and_keeps_residual_candidate(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    current = _rle(["....", ".##.", ".##.", "...."])
    candidate = _rle(["....", ".###", ".###", "...."])
    region = _rle(["....", "...#", "....", "...."])
    objects = _patch_mask_storage(monkeypatch, current, candidate, region)
    _task, _item, annotation, job, issue, row = await _seed_region_review(
        db_session,
        owner_id=user.id,
        current=current,
        candidate=candidate,
        region=region,
    )
    payload = _decision(annotation, job, issue, row["geometry_digest"])

    response = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json=payload,
        headers=_bearer(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "partially_reviewed"
    assert response.json()["revision"] == 2
    await db_session.refresh(annotation)
    await db_session.refresh(job)
    result_ref = annotation.geometry["keyframes"][0]["mask"]
    assert _rows(objects[result_ref["sha256"]]) == ["....", ".###", ".##.", "...."]
    assert len(job.staged_result["results"]) == 1
    assert job.staged_result["results"][0]["geometry_digest"] == row["geometry_digest"]

    scope = (
        await db_session.execute(
            select(MaskReviewScope).where(MaskReviewScope.source_job_id == job.id)
        )
    ).scalar_one()
    assert scope.source_annotation_version == 1
    assert scope.result_annotation_version == 2
    assert scope.region_digest == issue.region_digest

    replay = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json=payload,
        headers=_bearer(token),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["review_replayed"] is True
    assert replay.json()["revision"] == 2


async def test_region_reject_consumes_only_region_and_rotates_candidate_digest(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    current = _rle(["....", ".##.", ".##.", "...."])
    candidate = _rle(["....", ".###", ".###", "...."])
    region = _rle(["....", "...#", "....", "...."])
    objects = _patch_mask_storage(monkeypatch, current, candidate, region)
    _task, _item, annotation, job, issue, row = await _seed_region_review(
        db_session,
        owner_id=user.id,
        current=current,
        candidate=candidate,
        region=region,
    )
    payload = {
        **_decision(annotation, job, issue, row["geometry_digest"]),
        "decision": "reject",
    }

    response = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json=payload,
        headers=_bearer(token),
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(annotation)
    await db_session.refresh(job)
    assert annotation.version == 1
    residual = job.staged_result["results"][0]
    assert residual["geometry_digest"] != row["geometry_digest"]
    assert _rows(objects[residual["geometry"]["mask"]["sha256"]]) == [
        "....",
        ".##.",
        ".###",
        "....",
    ]
    scope = (
        await db_session.execute(
            select(MaskReviewScope).where(MaskReviewScope.source_job_id == job.id)
        )
    ).scalar_one()
    assert scope.decision == "reject"
    assert scope.result_annotation_version == 1


async def test_region_decision_rechecks_manual_annotation_segment_and_reviewed_locks(
    httpx_client, super_admin, reviewer, db_session, monkeypatch
):
    user, token = super_admin
    other_user, _other_token = reviewer
    user_id = user.id
    other_user_id = other_user.id
    current = _rle(["....", ".##.", ".##.", "...."])
    candidate = _rle(["....", ".###", ".##.", "...."])
    region = _rle(["....", "...#", "....", "...."])
    _patch_mask_storage(monkeypatch, current, candidate, region)

    _task, _item, annotation, job, issue, row = await _seed_region_review(
        db_session,
        owner_id=user_id,
        current=current,
        candidate=candidate,
        region=region,
        source="manual",
    )
    job_id = job.id
    dataset_item_id = job.dataset_item_id
    payload = _decision(annotation, job, issue, row["geometry_digest"])
    response = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job_id}/decisions",
        json=payload,
        headers=_bearer(token),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "manual_keyframe_protected"

    _task, _item, annotation, job, issue, row = await _seed_region_review(
        db_session,
        owner_id=user_id,
        current=current,
        candidate=candidate,
        region=region,
    )
    job_id = job.id
    dataset_item_id = job.dataset_item_id
    payload = _decision(annotation, job, issue, row["geometry_digest"])
    annotation.is_locked = True
    await db_session.commit()
    response = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job_id}/decisions",
        json=payload,
        headers=_bearer(token),
    )
    assert response.status_code == 409
    _task, _item, annotation, job, issue, row = await _seed_region_review(
        db_session,
        owner_id=user_id,
        current=current,
        candidate=candidate,
        region=region,
    )
    job_id = job.id
    payload = _decision(annotation, job, issue, row["geometry_digest"])
    scope = MaskReviewScope(
        project_id=issue.project_id,
        task_id=issue.task_id,
        annotation_id=annotation.id,
        qc_issue_id=issue.id,
        source_job_id=job.id,
        reviewer_id=user_id,
        source_annotation_version=1,
        result_annotation_version=1,
        source_job_revision=1,
        frame_start=1,
        frame_end=1,
        region_mask_ref=issue.region_mask_ref,
        region_digest=issue.region_digest,
        candidate_digest=row["geometry_digest"],
        decision="reject",
    )
    db_session.add(scope)
    await db_session.commit()
    response = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job_id}/decisions",
        json=payload,
        headers=_bearer(token),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "reviewed_scope_protected"

    _task, _item, annotation, job, issue, row = await _seed_region_review(
        db_session,
        owner_id=user_id,
        current=current,
        candidate=candidate,
        region=region,
    )
    job_id = job.id
    dataset_item_id = job.dataset_item_id
    payload = _decision(annotation, job, issue, row["geometry_digest"])
    segment = VideoSegment(
        dataset_item_id=dataset_item_id,
        segment_index=0,
        start_frame=0,
        end_frame=2,
        locked_by=other_user_id,
        lock_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db_session.add(segment)
    await db_session.flush()
    job.segment_id = segment.id
    await db_session.commit()
    response = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job_id}/decisions",
        json=payload,
        headers=_bearer(token),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "segment_lease_changed"


async def test_legacy_scope_guard_ignores_unselected_gap_frames(
    super_admin, db_session, monkeypatch
):
    user, _token = super_admin
    current = _rle(["....", ".##.", ".##.", "...."])
    candidate = _rle(["....", ".###", ".##.", "...."])
    region = _rle(["....", "...#", "....", "...."])
    _patch_mask_storage(monkeypatch, current, candidate, region)
    _task, _item, annotation, job, issue, row = await _seed_region_review(
        db_session,
        owner_id=user.id,
        current=current,
        candidate=candidate,
        region=region,
    )
    db_session.add(
        MaskReviewScope(
            project_id=issue.project_id,
            task_id=issue.task_id,
            annotation_id=annotation.id,
            qc_issue_id=issue.id,
            source_job_id=job.id,
            reviewer_id=user.id,
            source_annotation_version=1,
            result_annotation_version=1,
            source_job_revision=1,
            frame_start=2,
            frame_end=2,
            region_mask_ref=issue.region_mask_ref,
            region_digest=issue.region_digest,
            candidate_digest=row["geometry_digest"],
            decision="accept",
        )
    )
    await db_session.commit()

    await runner._assert_review_scopes_available(
        db_session,
        annotation=annotation,
        from_frame=1,
        to_frame=3,
        frame_indices={1, 3},
    )


async def test_claimed_reviewer_can_decide_job_created_by_annotator(
    httpx_client, super_admin, reviewer, db_session, monkeypatch
):
    owner, _owner_token = super_admin
    reviewer_user, reviewer_token = reviewer
    current = _rle(["....", ".##.", ".##.", "...."])
    candidate = _rle(["....", ".###", ".##.", "...."])
    region = _rle(["....", "...#", "....", "...."])
    _patch_mask_storage(monkeypatch, current, candidate, region)
    task, item, annotation, job, issue, row = await _seed_region_review(
        db_session,
        owner_id=owner.id,
        current=current,
        candidate=candidate,
        region=region,
    )
    await create_membership(
        db_session,
        project_id=task.project_id,
        user_id=reviewer_user.id,
        role="reviewer",
        assigned_by=owner.id,
    )
    batch = TaskBatch(
        project_id=task.project_id,
        dataset_id=item.dataset_id,
        display_id=f"B-REGION-{uuid.uuid4().hex[:8]}",
        name="Region review",
        status="reviewing",
        created_by=owner.id,
        reviewer_id=reviewer_user.id,
    )
    db_session.add(batch)
    await db_session.flush()
    task.batch_id = batch.id
    task.status = "review"
    task.reviewer_id = reviewer_user.id
    task.reviewer_claimed_at = datetime.now(timezone.utc)
    # Frozen review evidence: the reviewer is not a contributor, so the
    # self-review guard admits a legitimate claimed-reviewer decision.
    task.annotation_contributor_ids = [str(owner.id)]
    task.review_round_id = uuid.uuid4()
    task.review_contributor_ids = [str(owner.id)]
    task.review_submitter_id = str(owner.id)
    await db_session.commit()

    payload = {
        **_decision(annotation, job, issue, row["geometry_digest"]),
        "decision": "reject",
    }
    response = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json=payload,
        headers=_bearer(reviewer_token),
    )
    assert response.status_code == 200, response.text

    await db_session.execute(
        text("DELETE FROM mask_review_scopes WHERE source_job_id = :id"), {"id": job.id}
    )
    job.status = VideoTrackerJobStatus.PENDING_REVIEW.value
    job.revision = 1
    job.prompt = {"expected_source_versions": {str(annotation.id): 1}}
    job.staged_result = {
        "results": [row],
        "grid_step": 1,
        "output_geometry": "mask",
        "source_instance_id": "1",
    }
    task.reviewer_claimed_at = None
    await db_session.commit()
    response = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json=payload,
        headers=_bearer(reviewer_token),
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "task_review_not_claimed_by_user"

    task.status = "completed"
    task.reviewer_claimed_at = datetime.now(timezone.utc)
    await db_session.commit()
    response = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json=payload,
        headers=_bearer(reviewer_token),
    )
    # The entry route's phase capability guard rejects a completed task before
    # the service status guard, so the response is 403 with the canonical
    # missing-capability detail (annotation.write).
    assert response.status_code == 403, response.text
    assert "annotation.write" in str(response.json()["detail"]), response.text


async def test_migration_0145_has_review_scope_guards(db_session):
    constraints = set(
        (
            await db_session.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'mask_review_scopes'::regclass"
                )
            )
        ).scalars()
    )
    assert {
        "ck_mask_review_scopes_decision",
        "ck_mask_review_scopes_versions",
        "ck_mask_review_scopes_frames",
        "ck_mask_review_scopes_digests",
    } <= constraints
