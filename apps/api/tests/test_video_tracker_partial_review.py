from __future__ import annotations

from sqlalchemy import func, select

from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.video_tracker_job import VideoTrackerJob, VideoTrackerJobStatus
from tests.test_video_tracker_jobs_list import _bearer, _make_video_task


def _candidate(frame_index: int) -> dict:
    return {
        "frame_index": frame_index,
        "geometry": {
            "type": "bbox",
            "x": float(frame_index),
            "y": 0.0,
            "w": 5.0,
            "h": 5.0,
        },
        "confidence": 0.9,
        "outside": False,
        "instance_id": None,
        "primary": False,
    }


def _outside_candidate(frame_index: int, instance_id: str) -> dict:
    return {
        **_candidate(frame_index),
        "geometry": {"type": "bbox", "x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0},
        "confidence": 0.0,
        "outside": True,
        "instance_id": instance_id,
    }


async def _seed_review(db, owner_id, *, manual_frame: int | None = None):
    task, item = await _make_video_task(db, owner_id)
    keyframes = []
    if manual_frame is not None:
        keyframes.append(
            {
                "frame_index": manual_frame,
                "bbox": {"x": 20.0, "y": 0.0, "w": 5.0, "h": 5.0},
                "source": "manual",
                "occluded": False,
            }
        )
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=owner_id,
        annotation_type="video_track_bbox" if keyframes else "bbox",
        class_name="car",
        tool_unit_id="bbox",
        geometry=(
            {
                "type": "video_track_bbox",
                "track_id": "track-source",
                "keyframes": keyframes,
                "outside": [],
            }
            if keyframes
            else {"type": "bbox", "x": 1.0, "y": 2.0, "w": 5.0, "h": 5.0}
        ),
        track_id="track-source" if keyframes else None,
    )
    db.add(annotation)
    await db.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=owner_id,
        status=VideoTrackerJobStatus.PENDING_REVIEW.value,
        model_key="sam2_video",
        direction="forward",
        from_frame=0,
        to_frame=3,
        prompt={
            "expected_source_versions": {str(annotation.id): int(annotation.version)}
        },
        staged_result={
            "results": [_candidate(frame) for frame in (1, 2, 3)],
            "grid_step": 1,
            "output_geometry": "bbox",
        },
        event_channel="video-tracker-job:test",
    )
    db.add(job)
    await db.commit()
    return task, annotation, job


async def test_partial_accept_isolates_window_and_repeat_is_idempotent(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    _, annotation, job = await _seed_review(db_session, user.id)
    preview = await httpx_client.get(
        f"/api/v1/video-tracker-jobs/{job.id}/preview", headers=_bearer(token)
    )
    assert preview.status_code == 200, preview.text
    before = preview.json()
    assert before["job_revision"] == 1
    assert before["candidate_pending"] == 3
    assert all(item["candidate_key"].startswith("1:") for item in before["results"])

    payload = {
        "instance_ids": ["1"],
        "from_frame": 1,
        "to_frame": 1,
        "decision": "accept",
        "expected_source_versions": {str(annotation.id): int(annotation.version)},
        "job_revision": 1,
        "override_manual": False,
    }
    accepted = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json=payload,
        headers=_bearer(token),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "partially_reviewed"
    assert accepted.json()["revision"] == 2
    assert accepted.json()["review_replayed"] is False

    await db_session.refresh(annotation)
    assert [item["frame_index"] for item in annotation.geometry["keyframes"]] == [0, 1]
    assert annotation.geometry["keyframes"][1]["source"] == "prediction"

    after = await httpx_client.get(
        f"/api/v1/video-tracker-jobs/{job.id}/preview", headers=_bearer(token)
    )
    body = after.json()
    assert body["job_revision"] == 2
    assert body["candidate_pending"] == 2
    assert body["candidate_accepted"] == 1
    assert [item["frame_index"] for item in body["results"]] == [2, 3]
    assert body["expected_source_versions"][str(annotation.id)] == 2

    replay = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json=payload,
        headers=_bearer(token),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["review_replayed"] is True
    assert replay.json()["revision"] == 2
    audit_count = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.action == "video_tracker_job.decision",
            AuditLog.target_id == str(job.id),
        )
    )
    assert audit_count == 1


async def test_reject_finishes_mixed_review_without_touching_remaining_frames(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    _, annotation, job = await _seed_review(db_session, user.id)
    accept = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json={
            "instance_ids": ["1"],
            "from_frame": 1,
            "to_frame": 1,
            "decision": "accept",
            "expected_source_versions": {str(annotation.id): 1},
            "job_revision": 1,
        },
        headers=_bearer(token),
    )
    assert accept.status_code == 200, accept.text
    reject = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json={
            "instance_ids": ["1"],
            "from_frame": 2,
            "to_frame": 3,
            "decision": "reject",
            "expected_source_versions": {str(annotation.id): 2},
            "job_revision": 2,
        },
        headers=_bearer(token),
    )
    assert reject.status_code == 200, reject.text
    assert reject.json()["status"] == "accepted"
    assert reject.json()["revision"] == 3
    await db_session.refresh(annotation)
    assert [item["frame_index"] for item in annotation.geometry["keyframes"]] == [0, 1]


async def test_manual_keyframe_requires_override_and_audits_digests(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    _, annotation, job = await _seed_review(db_session, user.id, manual_frame=1)
    job_id = job.id
    payload = {
        "instance_ids": ["1"],
        "from_frame": 1,
        "to_frame": 1,
        "decision": "accept",
        "expected_source_versions": {str(annotation.id): 1},
        "job_revision": 1,
        "override_manual": False,
    }
    blocked = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job_id}/decisions",
        json=payload,
        headers=_bearer(token),
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["reason"] == "manual_keyframe_protected"
    await db_session.refresh(annotation)
    assert annotation.geometry["keyframes"][0]["source"] == "manual"

    overridden = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job_id}/decisions",
        json={**payload, "override_manual": True},
        headers=_bearer(token),
    )
    assert overridden.status_code == 200, overridden.text
    await db_session.refresh(annotation)
    assert annotation.geometry["keyframes"][0]["source"] == "prediction"
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "video_tracker_job.decision",
                AuditLog.target_id == str(job_id),
            )
        )
    ).scalar_one()
    override = audit.detail_json["manual_overrides"][0]
    assert override["before_digest"].startswith("sha256:")
    assert override["after_digest"].startswith("sha256:")
    assert override["before_digest"] != override["after_digest"]


async def test_stale_job_revision_is_structured_conflict(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    _, annotation, job = await _seed_review(db_session, user.id)
    response = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json={
            "instance_ids": ["1"],
            "from_frame": 1,
            "to_frame": 1,
            "decision": "reject",
            "expected_source_versions": {str(annotation.id): 1},
            "job_revision": 99,
        },
        headers=_bearer(token),
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "job_revision_conflict"
    assert detail["conflicts"] == [{"expected_revision": 99, "current_revision": 1}]


async def test_discovered_instance_reuses_one_annotation_across_windows(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=None,
        created_by=user.id,
        status=VideoTrackerJobStatus.PENDING_REVIEW.value,
        model_key="sam3_video",
        direction="forward",
        from_frame=0,
        to_frame=2,
        target_class_name="car",
        target_tool_unit_id="bbox",
        prompt={"expected_source_versions": {}},
        staged_result={
            "results": [
                {**_candidate(1), "instance_id": "new-1"},
                {**_candidate(2), "instance_id": "new-1"},
            ],
            "grid_step": 1,
            "output_geometry": "bbox",
        },
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()

    first = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json={
            "instance_ids": ["new-1"],
            "from_frame": 1,
            "to_frame": 1,
            "decision": "accept",
            "expected_source_versions": {},
            "job_revision": 1,
        },
        headers=_bearer(token),
    )
    assert first.status_code == 200, first.text
    preview = await httpx_client.get(
        f"/api/v1/video-tracker-jobs/{job.id}/preview", headers=_bearer(token)
    )
    body = preview.json()
    target_id = body["results"][0]["target_annotation_id"]
    target_version = body["expected_source_versions"][target_id]

    second = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json={
            "instance_ids": ["new-1"],
            "from_frame": 2,
            "to_frame": 2,
            "decision": "accept",
            "expected_source_versions": {target_id: target_version},
            "job_revision": 2,
        },
        headers=_bearer(token),
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "accepted"
    annotations = (
        (
            await db_session.execute(
                select(Annotation).where(
                    Annotation.task_id == task.id,
                    Annotation.source == "ai_tracker",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(annotations) == 1
    assert [item["frame_index"] for item in annotations[0].geometry["keyframes"]] == [
        1,
        2,
    ]


async def test_discovered_instance_keeps_outside_frames_accepted_before_visibility(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=None,
        created_by=user.id,
        status=VideoTrackerJobStatus.PENDING_REVIEW.value,
        model_key="sam3_video",
        direction="forward",
        from_frame=0,
        to_frame=2,
        target_class_name="car",
        target_tool_unit_id="bbox",
        prompt={"expected_source_versions": {}},
        staged_result={
            "results": [
                {**_candidate(0), "instance_id": "new-1"},
                _outside_candidate(1, "new-1"),
                {**_candidate(2), "instance_id": "new-1"},
            ],
            "grid_step": 1,
            "output_geometry": "bbox",
        },
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()

    outside = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json={
            "instance_ids": ["new-1"],
            "from_frame": 1,
            "to_frame": 1,
            "decision": "accept",
            "expected_source_versions": {},
            "job_revision": 1,
        },
        headers=_bearer(token),
    )
    assert outside.status_code == 200, outside.text
    assert outside.json()["status"] == "partially_reviewed"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Annotation)
            .where(Annotation.task_id == task.id, Annotation.source == "ai_tracker")
        )
        == 0
    )

    visible = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json={
            "instance_ids": ["new-1"],
            "from_frame": 0,
            "to_frame": 2,
            "decision": "accept",
            "expected_source_versions": {},
            "job_revision": 2,
        },
        headers=_bearer(token),
    )
    assert visible.status_code == 200, visible.text
    assert visible.json()["status"] == "accepted"
    annotation = (
        await db_session.execute(
            select(Annotation).where(
                Annotation.task_id == task.id,
                Annotation.source == "ai_tracker",
            )
        )
    ).scalar_one()
    assert [item["frame_index"] for item in annotation.geometry["keyframes"]] == [
        0,
        2,
    ]
    assert annotation.geometry["outside"] == [
        {"from": 1, "to": 1, "source": "prediction"}
    ]


async def test_sourceless_decision_skips_instance_with_only_outside_candidates(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
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
        target_class_name="bus",
        target_tool_unit_id="bbox",
        prompt={"expected_source_versions": {}},
        staged_result={
            "results": [
                {**_candidate(0), "instance_id": "visible"},
                {**_candidate(1), "instance_id": "visible"},
                _outside_candidate(0, "missing"),
                _outside_candidate(1, "missing"),
            ],
            "grid_step": 1,
            "output_geometry": "bbox",
        },
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()

    accepted = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json={
            "instance_ids": ["visible", "missing"],
            "from_frame": 0,
            "to_frame": 1,
            "decision": "accept",
            "expected_source_versions": {},
            "job_revision": 1,
        },
        headers=_bearer(token),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["prompt"]["review_state"]["instance_annotations"].keys() == {
        "visible"
    }

    annotations = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/annotations", headers=_bearer(token)
    )
    assert annotations.status_code == 200, annotations.text
    ai_tracks = [row for row in annotations.json() if row["source"] == "ai_tracker"]
    assert len(ai_tracks) == 1
    assert ai_tracks[0]["geometry"]["keyframes"]


async def test_selector_overlapping_opposite_decision_is_rejected(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    _, annotation, job = await _seed_review(db_session, user.id)
    first = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json={
            "instance_ids": ["1"],
            "from_frame": 1,
            "to_frame": 1,
            "decision": "reject",
            "expected_source_versions": {str(annotation.id): 1},
            "job_revision": 1,
        },
        headers=_bearer(token),
    )
    assert first.status_code == 200, first.text

    overlapping = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json={
            "instance_ids": ["1"],
            "from_frame": 1,
            "to_frame": 2,
            "decision": "accept",
            "expected_source_versions": {str(annotation.id): 1},
            "job_revision": 2,
        },
        headers=_bearer(token),
    )
    assert overlapping.status_code == 409, overlapping.text
    assert overlapping.json()["detail"]["reason"] == "candidate_decision_conflict"


async def test_legacy_single_source_mapping_survives_primary_slice_removal(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    source = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="video_track_bbox",
        class_name="car",
        tool_unit_id="bbox",
        geometry={
            "type": "video_track_bbox",
            "track_id": "track-source",
            "keyframes": [],
            "outside": [],
        },
        track_id="track-source",
    )
    db_session.add(source)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=source.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.PENDING_REVIEW.value,
        model_key="sam3_video",
        direction="forward",
        from_frame=1,
        to_frame=2,
        prompt={"expected_source_versions": {str(source.id): 1}},
        staged_result={
            # Legacy staged payloads do not carry source_instance_id.
            "results": [
                {**_candidate(1), "instance_id": "source", "primary": True},
                {**_candidate(2), "instance_id": "new", "primary": False},
            ],
            "grid_step": 1,
            "output_geometry": "bbox",
        },
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()

    accepted = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/decisions",
        json={
            "instance_ids": ["source"],
            "from_frame": 1,
            "to_frame": 1,
            "decision": "accept",
            "expected_source_versions": {str(source.id): 1},
            "job_revision": 1,
        },
        headers=_bearer(token),
    )
    assert accepted.status_code == 200, accepted.text

    preview = await httpx_client.get(
        f"/api/v1/video-tracker-jobs/{job.id}/preview", headers=_bearer(token)
    )
    assert preview.status_code == 200, preview.text
    remaining = preview.json()["results"]
    assert len(remaining) == 1
    assert remaining[0]["instance_id"] == "new"
    assert remaining[0]["source_annotation_id"] is None
    assert remaining[0]["target_annotation_id"] is None
