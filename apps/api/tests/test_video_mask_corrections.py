from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.task_lock import TaskLock
from app.db.models.video_tracker_job import VideoTrackerJob, VideoTrackerJobStatus
from app.schemas.video_tracker_job import (
    VideoMaskKeyframeOperationRequest,
    VideoMaskKeyframeSaveRequest,
    VideoTrackerJobOut,
)
from app.services.audit import AuditAction
from app.services.raster_mask_storage import build_rle_reference
from app.services.video_tracking.jobs import (
    operate_video_mask_keyframe,
    save_video_mask_keyframe,
)
from app.services.video_tracking.runner import (
    _correction_execution_windows,
    _correction_seed,
)
from tests.test_ai_mask_accept import _seed
from tests.test_video_tracker_jobs_list import _bearer


RLE = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 3]}
ALT_RLE = {"encoding": "coco_rle", "size": [2, 3], "counts": [2, 1, 3]}


@pytest.fixture
def correction_storage_mocks(monkeypatch):
    validate = AsyncMock()
    lock = AsyncMock()
    load = AsyncMock(return_value=RLE)
    monkeypatch.setattr(
        "app.services.video_tracking.jobs.validate_mask_geometry_for_task",
        validate,
    )
    monkeypatch.setattr(
        "app.services.video_tracking.jobs.lock_raster_mask_references",
        lock,
    )
    monkeypatch.setattr(
        "app.services.video_tracking.jobs.load_coco_rle",
        load,
    )
    monkeypatch.setattr(
        "app.services.video_tracking.runner.load_coco_rle",
        load,
    )
    return validate, lock, load


async def _seed_mask_track(
    db,
    owner_id,
    *,
    frame_index: int = 5,
    source: str = "manual",
    version: int = 3,
    native_mask: bool = True,
):
    task, backend, pool = await _seed(db, owner_id=owner_id, media_type="video")
    project = await db.get(Project, task.project_id)
    model_id = "grounded-sam2-tracker" if native_mask else "sam3-video-tracker"
    model_key = "sam2_video" if native_mask else "sam3_video"
    backend.health_meta = {
        "capabilities": {
            "supported_trackers": [model_key],
            "models": [
                {
                    "id": model_id,
                    "task": "tracker",
                    "supported_trackers": [model_key],
                    "text_driven_trackers": [] if native_mask else [model_key],
                    "supported_prompts": ["correction_frame"]
                    if native_mask
                    else ["text"],
                    "supported_inputs": (
                        ["video", "mask_prompt"]
                        if native_mask
                        else ["video", "bbox_prompt"]
                    ),
                    "supported_geometric_outputs": ["mask"],
                    "max_window_frames": 16,
                }
            ],
        }
    }
    project.ml_backend_pool_id = pool.id
    db.add(
        ProjectMLBackendPool(
            project_id=project.id,
            pool_id=pool.id,
            enabled=True,
        )
    )
    reference = build_rle_reference(RLE)
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=owner_id,
        source="manual",
        annotation_type="video_track_mask",
        tool_unit_id="region",
        class_name="object",
        track_id="mask-track-1",
        geometry={
            "type": "video_track_mask",
            "track_id": "mask-track-1",
            "keyframes": [
                {
                    "frame_index": frame_index,
                    "mask": reference,
                    "source": source,
                    "occluded": False,
                }
            ],
            "outside": [{"from": 0, "to": 8, "source": "prediction"}],
        },
        version=version,
    )
    db.add(annotation)
    await db.flush()
    return task, backend, model_id, model_key, annotation, reference


async def _passthrough_enqueue(db, row_id, *, fail_closed=False):
    del fail_closed
    row = await db.get(VideoTrackerJob, row_id)
    assert row is not None
    return VideoTrackerJobOut.model_validate(row, from_attributes=True)


async def test_save_frame_zero_is_surgical_versioned_and_audited(
    httpx_client,
    db_session,
    super_admin,
    correction_storage_mocks,
):
    user, token = super_admin
    (
        task,
        _backend,
        _model_id,
        _model_key,
        annotation,
        _reference,
    ) = await _seed_mask_track(
        db_session,
        user.id,
        frame_index=4,
        version=1,
    )
    new_reference = build_rle_reference(ALT_RLE)

    missing = await httpx_client.put(
        f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}/mask-keyframes/0",
        json={"mask": new_reference},
        headers=_bearer(token),
    )
    assert missing.status_code == 428
    assert missing.json()["detail"]["reason"] == "if_match_required"

    saved = await httpx_client.put(
        f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}/mask-keyframes/0",
        json={"mask": new_reference},
        headers={**_bearer(token), "If-Match": 'W/"1"'},
    )
    assert saved.status_code == 200, saved.text
    assert saved.headers["etag"] == 'W/"2"'
    assert saved.json()["version"] == 2
    await db_session.refresh(annotation)
    assert [item["frame_index"] for item in annotation.geometry["keyframes"]] == [0, 4]
    assert annotation.geometry["keyframes"][0]["source"] == "manual"
    assert (
        annotation.geometry["keyframes"][0]["mask"]["sha256"] == new_reference["sha256"]
    )
    assert annotation.geometry["outside"] == [
        {"from": 1, "to": 8, "source": "prediction"}
    ]
    validate, lock, _load = correction_storage_mocks
    validate.assert_awaited_once()
    lock.assert_awaited_once()

    stale = await httpx_client.put(
        f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}/mask-keyframes/0",
        json={"mask": new_reference},
        headers={**_bearer(token), "If-Match": 'W/"1"'},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "reason": "source_version_conflict",
        "current_version": 2,
    }
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == AuditAction.VIDEO_MASK_KEYFRAME_CORRECT,
                AuditLog.target_id == str(annotation.id),
            )
        )
    ).scalar_one()
    assert audit.detail_json["frame_index"] == 0
    assert audit.detail_json["source_version"] == 1
    assert audit.detail_json["result_version"] == 2


async def test_save_mask_keyframe_holds_task_lock_before_rle_lock(
    monkeypatch,
):
    task_id = uuid.uuid4()
    annotation_id = uuid.uuid4()
    reference = build_rle_reference(RLE)
    annotation = SimpleNamespace(
        id=annotation_id,
        task_id=task_id,
        is_active=True,
        is_locked=False,
        version=1,
        track_id="mask-track-1",
        geometry={
            "type": "video_track_mask",
            "track_id": "mask-track-1",
            "keyframes": [
                {
                    "frame_index": 4,
                    "mask": reference,
                    "source": "manual",
                    "occluded": False,
                }
            ],
            "outside": [],
        },
    )
    task = SimpleNamespace(id=task_id, project_id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4())
    ctx = SimpleNamespace(metadata=SimpleNamespace(frame_count=12))
    events: list[str] = []

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    results = iter([task, annotation, annotation])

    async def execute(_statement):
        value = next(results)
        if value is task:
            events.append("task")
        elif events == ["task", "task-edit-lock"]:
            events.append("annotation-read")
        else:
            events.append("annotation-lock")
        return _Result(value)

    db = SimpleNamespace(
        execute=AsyncMock(side_effect=execute),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.video_tracking.jobs._is_privileged",
        AsyncMock(return_value=True),
    )

    async def task_write_lock(*_args, **_kwargs):
        events.append("task-edit-lock")

    monkeypatch.setattr(
        "app.services.video_tracking.jobs._assert_task_write_allowed",
        task_write_lock,
    )

    async def segment_lock(*_args, **_kwargs):
        events.append("segment")
        return uuid.uuid4()

    async def validate(*_args, **_kwargs):
        events.append("validate")

    async def content_lock(*_args, **_kwargs):
        events.append("rle")

    monkeypatch.setattr(
        "app.services.video_tracking.jobs._assert_segment_lock", segment_lock
    )
    monkeypatch.setattr(
        "app.services.video_tracking.jobs.validate_mask_geometry_for_task", validate
    )
    monkeypatch.setattr(
        "app.services.video_tracking.jobs.lock_raster_mask_references", content_lock
    )

    saved, _detail = await save_video_mask_keyframe(
        db,
        task=task,
        ctx=ctx,
        annotation_id=annotation_id,
        frame_index=0,
        payload=VideoMaskKeyframeSaveRequest(
            mask=build_rle_reference(ALT_RLE), source="prediction"
        ),
        expected_version=1,
        user=user,
    )

    assert events == [
        "task",
        "task-edit-lock",
        "annotation-read",
        "segment",
        "validate",
        "rle",
        "annotation-lock",
    ]
    assert saved.version == 2
    assert saved.geometry["keyframes"][0]["source"] == "prediction"


async def test_mask_keyframe_delete_is_versioned_surgical_and_restores_hold(
    httpx_client,
    db_session,
    super_admin,
):
    user, token = super_admin
    (
        task,
        _backend,
        _model_id,
        _model_key,
        annotation,
        reference,
    ) = await _seed_mask_track(db_session, user.id, frame_index=2, version=3)
    other_reference = build_rle_reference(ALT_RLE)
    annotation.geometry = {
        **annotation.geometry,
        "keyframes": [
            {
                "frame_index": 2,
                "mask": reference,
                "source": "manual",
                "occluded": False,
            },
            {
                "frame_index": 8,
                "mask": other_reference,
                "source": "manual",
                "occluded": True,
            },
        ],
        "outside": [],
    }
    await db_session.flush()
    await db_session.refresh(annotation)
    current_version = annotation.version
    path = f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}/mask-keyframes/8"

    missing = await httpx_client.patch(
        path,
        json={"operation": "delete_keyframe"},
        headers=_bearer(token),
    )
    assert missing.status_code == 428

    deleted = await httpx_client.patch(
        path,
        json={"operation": "delete_keyframe"},
        headers={**_bearer(token), "If-Match": f'W/"{current_version}"'},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.headers["etag"] == f'W/"{current_version + 1}"'
    assert deleted.headers["x-resolved-keyframe-frame"] == "2"
    assert deleted.headers["x-restored-held"] == "true"
    await db_session.refresh(annotation)
    assert annotation.version == current_version + 1
    assert annotation.geometry["keyframes"] == [
        {
            "frame_index": 2,
            "mask": reference,
            "source": "manual",
            "occluded": False,
        }
    ]
    assert annotation.geometry["outside"] == []

    last = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}/mask-keyframes/2",
        json={"operation": "delete_keyframe"},
        headers={**_bearer(token), "If-Match": f'W/"{current_version + 1}"'},
    )
    assert last.status_code == 422
    assert last.json()["detail"]["reason"] == "last_keyframe_required"

    stale = await httpx_client.patch(
        path,
        json={"operation": "mark_outside"},
        headers={**_bearer(token), "If-Match": f'W/"{current_version}"'},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "reason": "source_version_conflict",
        "current_version": current_version + 1,
    }
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == AuditAction.VIDEO_MASK_KEYFRAME_OPERATE,
                AuditLog.target_id == str(annotation.id),
            )
        )
    ).scalar_one()
    assert audit.detail_json["operation"] == "delete_keyframe"
    assert audit.detail_json["resolved_keyframe_frame"] == 2


async def test_mask_manual_outside_restore_preserves_prediction_and_keyframes(
    httpx_client,
    db_session,
    super_admin,
):
    user, token = super_admin
    (
        task,
        _backend,
        _model_id,
        _model_key,
        annotation,
        reference,
    ) = await _seed_mask_track(db_session, user.id, frame_index=2, version=5)
    other_reference = build_rle_reference(ALT_RLE)
    keyframes = [
        {
            "frame_index": 2,
            "mask": reference,
            "source": "manual",
            "occluded": False,
        },
        {
            "frame_index": 8,
            "mask": other_reference,
            "source": "prediction",
            "occluded": False,
        },
    ]
    annotation.geometry = {
        **annotation.geometry,
        "keyframes": keyframes,
        "outside": [{"from": 0, "to": 3, "source": "prediction"}],
    }
    await db_session.flush()
    await db_session.refresh(annotation)
    current_version = annotation.version
    path = f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}/mask-keyframes/4"

    hidden = await httpx_client.patch(
        path,
        json={"operation": "mark_outside"},
        headers={**_bearer(token), "If-Match": f'W/"{current_version}"'},
    )
    assert hidden.status_code == 200, hidden.text
    await db_session.refresh(annotation)
    assert annotation.geometry["outside"] == [
        {"from": 0, "to": 3, "source": "prediction"},
        {"from": 4, "to": 4, "source": "manual"},
    ]
    assert annotation.geometry["keyframes"] == keyframes

    restored = await httpx_client.patch(
        path,
        json={"operation": "restore_held"},
        headers={**_bearer(token), "If-Match": f'W/"{current_version + 1}"'},
    )
    assert restored.status_code == 200, restored.text
    assert restored.headers["x-resolved-keyframe-frame"] == "8"
    assert restored.headers["x-restored-held"] == "true"
    await db_session.refresh(annotation)
    assert annotation.version == current_version + 2
    assert annotation.geometry["outside"] == [
        {"from": 0, "to": 3, "source": "prediction"}
    ]
    assert annotation.geometry["keyframes"] == keyframes

    prediction_only = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}/mask-keyframes/3",
        json={"operation": "restore_held"},
        headers={**_bearer(token), "If-Match": f'W/"{current_version + 2}"'},
    )
    assert prediction_only.status_code == 409
    assert prediction_only.json()["detail"]["reason"] == "manual_outside_missing"

    overlapping_manual = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}/mask-keyframes/3",
        json={"operation": "mark_outside"},
        headers={**_bearer(token), "If-Match": f'W/"{current_version + 2}"'},
    )
    assert overlapping_manual.status_code == 200, overlapping_manual.text
    await db_session.refresh(annotation)
    assert annotation.geometry["outside"] == [
        {"from": 0, "to": 3, "source": "prediction"},
        {"from": 3, "to": 3, "source": "manual"},
    ]

    restored_overlap = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}/mask-keyframes/3",
        json={"operation": "restore_held"},
        headers={**_bearer(token), "If-Match": f'W/"{current_version + 3}"'},
    )
    assert restored_overlap.status_code == 200, restored_overlap.text
    await db_session.refresh(annotation)
    assert annotation.geometry["outside"] == [
        {"from": 0, "to": 3, "source": "prediction"}
    ]


async def test_mask_keyframe_operations_honor_task_and_annotation_locks(
    httpx_client,
    db_session,
    super_admin,
    annotator,
):
    actor, token = super_admin
    lock_owner, _ = annotator
    (
        task,
        _backend,
        _model_id,
        _model_key,
        annotation,
        _reference,
    ) = await _seed_mask_track(db_session, actor.id, frame_index=5, version=1)
    db_session.add(
        TaskLock(
            task_id=task.id,
            user_id=lock_owner.id,
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    await db_session.flush()
    path = f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}/mask-keyframes/4"

    task_locked = await httpx_client.patch(
        path,
        json={"operation": "mark_outside"},
        headers={**_bearer(token), "If-Match": 'W/"1"'},
    )
    assert task_locked.status_code == 409
    assert task_locked.json()["detail"]["reason"] == "task_lock_conflict"

    await db_session.execute(
        TaskLock.__table__.delete().where(TaskLock.task_id == task.id)
    )
    annotation.is_locked = True
    await db_session.flush()
    annotation_locked = await httpx_client.patch(
        path,
        json={"operation": "mark_outside"},
        headers={**_bearer(token), "If-Match": 'W/"1"'},
    )
    assert annotation_locked.status_code == 409
    assert annotation_locked.json()["detail"]["reason"] == "annotation_locked"


async def test_operate_mask_keyframe_lock_order_is_task_segment_annotation(
    monkeypatch,
):
    task_id = uuid.uuid4()
    annotation_id = uuid.uuid4()
    reference = build_rle_reference(RLE)
    task = SimpleNamespace(id=task_id, project_id=uuid.uuid4())
    annotation = SimpleNamespace(
        id=annotation_id,
        task_id=task_id,
        is_active=True,
        is_locked=False,
        version=1,
        track_id="mask-track-1",
        geometry={
            "type": "video_track_mask",
            "track_id": "mask-track-1",
            "keyframes": [
                {
                    "frame_index": 4,
                    "mask": reference,
                    "source": "manual",
                    "occluded": False,
                }
            ],
            "outside": [],
        },
    )
    user = SimpleNamespace(id=uuid.uuid4())
    ctx = SimpleNamespace(metadata=SimpleNamespace(frame_count=12))
    events: list[str] = []

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    results = iter([task, annotation, annotation])

    async def execute(_statement):
        value = next(results)
        events.append(
            "task"
            if value is task
            else "annotation-read"
            if events[-1] == "task-edit-lock"
            else "annotation-lock"
        )
        return _Result(value)

    db = SimpleNamespace(
        execute=AsyncMock(side_effect=execute),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )

    async def task_write_lock(*_args, **_kwargs):
        events.append("task-edit-lock")

    async def segment_lock(*_args, **_kwargs):
        events.append("segment")
        return uuid.uuid4()

    monkeypatch.setattr(
        "app.services.video_tracking.jobs._assert_task_write_allowed",
        task_write_lock,
    )
    monkeypatch.setattr(
        "app.services.video_tracking.jobs._is_privileged",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.video_tracking.jobs._assert_segment_lock",
        segment_lock,
    )

    saved, _detail = await operate_video_mask_keyframe(
        db,
        task=task,
        ctx=ctx,
        annotation_id=annotation_id,
        frame_index=5,
        payload=VideoMaskKeyframeOperationRequest(operation="mark_outside"),
        expected_version=1,
        user=user,
    )

    assert events == [
        "task",
        "task-edit-lock",
        "annotation-read",
        "segment",
        "annotation-lock",
    ]
    assert saved.version == 2


async def test_create_correction_freezes_exact_route_and_single_active_lease(
    httpx_client,
    db_session,
    super_admin,
    correction_storage_mocks,
    monkeypatch,
):
    user, token = super_admin
    task, backend, model_id, model_key, annotation, reference = await _seed_mask_track(
        db_session,
        user.id,
    )
    monkeypatch.setattr(
        "app.api.v1.tasks.video.enqueue_tracker_job",
        _passthrough_enqueue,
    )
    payload = {
        "correction_frame": 5,
        "from_frame": 2,
        "to_frame": 8,
        "model_key": model_key,
        "model_id": model_id,
        "backend_id": str(backend.id),
        "direction": "bidirectional",
        "source_annotation_version": 3,
        "corrected_mask_digest": reference["sha256"],
    }

    created = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}/correction-jobs",
        json=payload,
        headers=_bearer(token),
    )
    assert created.status_code == 202, created.text
    body = created.json()
    assert body["job_kind"] == "correction"
    assert body["track_id_snapshot"] == "mask-track-1"
    correction = body["prompt"]["correction"]
    assert correction["source_version"] == 3
    assert correction["corrected_digest"] == reference["sha256"]
    assert correction["seed_mode"] == "native_mask"
    assert correction["routing"] == {
        "requested_backend_id": str(backend.id),
        "backend_pool_id": str(
            (await db_session.get(Project, task.project_id)).ml_backend_pool_id
        ),
        "model_id": model_id,
        "model_key": model_key,
        "max_window_frames": 16,
    }
    assert correction["segment"]["lease_enforced"] is False
    row = await db_session.get(VideoTrackerJob, uuid.UUID(body["id"]))
    assert row is not None
    assert _correction_execution_windows(row) == [
        (2, 5, "backward", True),
        (5, 8, "forward", True),
    ]
    assert await _correction_seed(db_session, row, annotation) == [
        {
            "obj_id": 1,
            "prompts": [
                {
                    "type": "correction_frame",
                    "frame_index": 5,
                    "direction": "bidirectional",
                    "mask_prompt": {
                        "rle": RLE,
                        "source_annotation_id": str(annotation.id),
                        "source_version": 3,
                        "source_digest": reference["sha256"],
                    },
                    "output_geometry": "mask",
                }
            ],
        }
    ]

    preview = await httpx_client.get(
        f"/api/v1/video-tracker-jobs/{body['id']}/preview",
        headers=_bearer(token),
    )
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert {
        key: preview_body[key]
        for key in (
            "job_kind",
            "correction_frame",
            "direction",
            "from_frame",
            "to_frame",
            "seed_mode",
            "protect_manual",
        )
    } == {
        "job_kind": "correction",
        "correction_frame": 5,
        "direction": "bidirectional",
        "from_frame": 2,
        "to_frame": 8,
        "seed_mode": "native_mask",
        "protect_manual": True,
    }

    duplicate = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}/correction-jobs",
        json=payload,
        headers=_bearer(token),
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["reason"] == "correction_job_active"


async def test_bbox_fallback_requires_exact_capability_confirmation_and_text(
    httpx_client,
    db_session,
    super_admin,
    correction_storage_mocks,
    monkeypatch,
):
    user, token = super_admin
    task, backend, model_id, model_key, annotation, reference = await _seed_mask_track(
        db_session,
        user.id,
        native_mask=False,
    )
    monkeypatch.setattr(
        "app.api.v1.tasks.video.enqueue_tracker_job",
        _passthrough_enqueue,
    )
    payload = {
        "correction_frame": 5,
        "from_frame": 5,
        "to_frame": 8,
        "model_key": model_key,
        "model_id": model_id,
        "backend_id": str(backend.id),
        "direction": "forward",
        "source_annotation_version": 3,
        "corrected_mask_digest": reference["sha256"],
    }
    unconfirmed = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}/correction-jobs",
        json=payload,
        headers=_bearer(token),
    )
    assert unconfirmed.status_code == 409
    assert unconfirmed.json()["detail"]["reason"] == "mask_prompt_unsupported"

    no_text = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}/correction-jobs",
        json={**payload, "allow_bbox_fallback": True},
        headers=_bearer(token),
    )
    assert no_text.status_code == 422
    assert no_text.json()["detail"]["reason"] == "text_required_for_bbox_fallback"

    created = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}/correction-jobs",
        json={**payload, "allow_bbox_fallback": True, "text": "car"},
        headers=_bearer(token),
    )
    assert created.status_code == 202, created.text
    correction = created.json()["prompt"]["correction"]
    assert correction["seed_mode"] == "bbox"
    assert correction["fallback_reason"] == "mask_prompt_unsupported"
    assert correction["fallback_confirmed"] is True
    assert correction["seed_bbox"] is not None
    row = await db_session.get(VideoTrackerJob, uuid.UUID(created.json()["id"]))
    assert row is not None
    assert await _correction_seed(db_session, row, annotation) == [
        {"obj_id": 1, "bbox": correction["seed_bbox"]}
    ]


async def test_enqueue_failure_marks_job_failed_and_releases_active_lease(
    httpx_client,
    db_session,
    super_admin,
    correction_storage_mocks,
    monkeypatch,
):
    user, token = super_admin
    task, backend, model_id, model_key, annotation, reference = await _seed_mask_track(
        db_session,
        user.id,
    )
    send_task = MagicMock(side_effect=RuntimeError("broker unavailable"))
    monkeypatch.setattr("celery.current_app.send_task", send_task)
    task_id = task.id
    annotation_id = annotation.id
    payload = {
        "correction_frame": 5,
        "from_frame": 5,
        "to_frame": 8,
        "model_key": model_key,
        "model_id": model_id,
        "backend_id": str(backend.id),
        "direction": "forward",
        "source_annotation_version": 3,
        "corrected_mask_digest": reference["sha256"],
    }

    failed = await httpx_client.post(
        f"/api/v1/tasks/{task_id}/video/tracks/{annotation_id}/correction-jobs",
        json=payload,
        headers=_bearer(token),
    )
    assert failed.status_code == 503
    assert failed.json()["detail"]["reason"] == "tracker_enqueue_failed"
    failed_job_id = uuid.UUID(failed.json()["detail"]["job_id"])
    failed_job = await db_session.get(VideoTrackerJob, failed_job_id)
    assert failed_job is not None
    assert failed_job.status == VideoTrackerJobStatus.FAILED.value
    assert failed_job.error_message == "tracker_enqueue_failed"

    monkeypatch.setattr(
        "app.api.v1.tasks.video.enqueue_tracker_job",
        _passthrough_enqueue,
    )
    retried = await httpx_client.post(
        f"/api/v1/tasks/{task_id}/video/tracks/{annotation_id}/correction-jobs",
        json=payload,
        headers=_bearer(token),
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["id"] != str(failed_job_id)


async def test_correction_cancel_preserves_manual_frame_and_blocks_bulk_review(
    httpx_client,
    db_session,
    super_admin,
    correction_storage_mocks,
    monkeypatch,
):
    user, token = super_admin
    task, backend, model_id, model_key, annotation, reference = await _seed_mask_track(
        db_session,
        user.id,
    )
    monkeypatch.setattr(
        "app.api.v1.tasks.video.enqueue_tracker_job",
        _passthrough_enqueue,
    )
    created = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}/correction-jobs",
        json={
            "correction_frame": 5,
            "from_frame": 5,
            "to_frame": 8,
            "model_key": model_key,
            "model_id": model_id,
            "backend_id": str(backend.id),
            "direction": "forward",
            "source_annotation_version": 3,
            "corrected_mask_digest": reference["sha256"],
        },
        headers=_bearer(token),
    )
    job_id = created.json()["id"]
    job = await db_session.get(VideoTrackerJob, uuid.UUID(job_id))
    assert job is not None
    job.status = VideoTrackerJobStatus.PENDING_REVIEW.value
    job.staged_result = {
        "results": [
            {
                "frame_index": 6,
                "instance_id": "1",
                "geometry": {"type": "mask", "mask": reference},
            }
        ],
        "output_geometry": "mask",
    }
    await db_session.commit()

    for action in ("accept", "discard"):
        blocked = await httpx_client.post(
            f"/api/v1/video-tracker-jobs/{job_id}/{action}",
            json={},
            headers=_bearer(token),
        )
        assert blocked.status_code == 409
        assert (
            blocked.json()["detail"]["reason"] == "correction_requires_local_decision"
        )

    cancelled = await httpx_client.delete(
        f"/api/v1/video-tracker-jobs/{job_id}",
        headers=_bearer(token),
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    await db_session.refresh(job)
    await db_session.refresh(annotation)
    assert job.staged_result is None
    assert annotation.version == 3
    assert annotation.geometry["keyframes"][0]["source"] == "manual"
    audit_count = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.action == AuditAction.VIDEO_CORRECTION_JOB_CANCEL,
            AuditLog.target_id == str(job.id),
        )
    )
    assert audit_count == 1

    repeated = await httpx_client.delete(
        f"/api/v1/video-tracker-jobs/{job_id}",
        headers=_bearer(token),
    )
    assert repeated.status_code == 200
    repeated_audit_count = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.action == AuditAction.VIDEO_CORRECTION_JOB_CANCEL,
            AuditLog.target_id == str(job.id),
        )
    )
    assert repeated_audit_count == 1
