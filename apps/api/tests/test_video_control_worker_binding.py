from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import make_transient_to_detached

from aap_labeling.video_control_contract import (
    LockWorkerJob,
    ReadWorkerJob,
    ReadWorkerSegment,
    StartWorkerJob,
)
from app.db.models.dataset import VideoSegment
from app.db.models.task import Task
from app.db.models.video_tracker_job import VideoTrackerJob
from app.services import labeling_core_read_relations
from app.services.video_tracking import control
from app.services.video_tracking import runner


def _job_row(job_id: uuid.UUID) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": job_id,
        "task_id": uuid.uuid4(),
        "dataset_item_id": uuid.uuid4(),
        "annotation_id": None,
        "segment_id": None,
        "target_class_name": None,
        "target_tool_unit_id": None,
        "created_by": uuid.uuid4(),
        "status": "queued",
        "job_kind": "tracking",
        "track_id_snapshot": None,
        "correction_frame": None,
        "model_key": "mock_bbox",
        "direction": "forward",
        "from_frame": 0,
        "to_frame": 1,
        "prompt": {},
        "staged_result": None,
        "revision": 1,
        "event_channel": f"video-tracker-job:{job_id}",
        "celery_task_id": None,
        "cancel_requested_at": None,
        "started_at": None,
        "completed_at": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }


@pytest.mark.asyncio
async def test_worker_job_commands_refresh_a_session_bound_identity(monkeypatch):
    job_id = uuid.uuid4()
    row = _job_row(job_id)
    calls = 0

    async def command(_db, request):
        nonlocal calls
        calls += 1
        assert request.job_id == job_id
        if isinstance(request, StartWorkerJob):
            return {
                **row,
                "status": "running",
                "celery_task_id": request.celery_task_id,
                "started_at": request.started_at,
            }
        return dict(row)

    monkeypatch.setattr(control, "video_control_command", command)
    session = AsyncSession()
    loaded = await control.video_control_job(
        session,
        ReadWorkerJob(
            schema="aap-labeling-video-control/v1",
            operation="read_worker_job",
            job_id=job_id,
        ),
    )
    assert isinstance(loaded, VideoTrackerJob)
    assert inspect(loaded).session is session.sync_session
    assert await runner._get_worker_job(session, job_id) is loaded
    assert calls == 1

    row["status"] = "running"
    row["celery_task_id"] = "worker-task"
    row["started_at"] = datetime.now(timezone.utc)
    updated = await control.video_control_job(
        session,
        StartWorkerJob(
            schema="aap-labeling-video-control/v1",
            operation="start_worker_job",
            job_id=job_id,
            celery_task_id="worker-task",
            started_at=row["started_at"],
        ),
    )
    assert updated is loaded
    assert updated.status == "running"
    assert updated.celery_task_id == "worker-task"
    assert updated.started_at == row["started_at"]
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("autoflush", [True, False])
async def test_worker_refresh_expires_target_before_autoflush_and_owner_read(
    monkeypatch, autoflush
):
    job_id = uuid.uuid4()
    row = _job_row(job_id)
    job = VideoTrackerJob(**row)
    make_transient_to_detached(job)
    session = AsyncSession(autoflush=autoflush)
    loaded = await session.merge(job, load=False)
    loaded.status = "local-edit"
    original_expire = session.expire
    original_expire(loaded, ["id"])
    events = []

    def expire(instance, attribute_names=None):
        events.append("expire")
        return original_expire(instance, attribute_names)

    async def flush():
        events.append("flush")
        assert not inspect(loaded).attrs.status.history.has_changes()

    async def owner_read(_db, request):
        events.append("read")
        assert isinstance(request, ReadWorkerJob)
        assert request.job_id == job_id
        refreshed = VideoTrackerJob(**{**row, "status": "running"})
        make_transient_to_detached(refreshed)
        return await session.merge(refreshed, load=False)

    monkeypatch.setattr(session, "expire", expire)
    monkeypatch.setattr(session, "flush", flush)
    monkeypatch.setattr(runner, "video_control_job", owner_read)
    refreshed = await runner._refresh_worker_job(session, loaded)
    assert refreshed is loaded
    assert refreshed.status == "running"
    assert events == (["expire", "flush", "read"] if autoflush else ["expire", "read"])

    async def missing(_db, _request):
        return None

    monkeypatch.setattr(runner, "video_control_job", missing)
    with pytest.raises(InvalidRequestError, match="Could not refresh instance"):
        await runner._refresh_worker_job(session, loaded)
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("autoflush", [True, False])
async def test_task_refresh_expires_before_0352_autoflush_and_read(
    monkeypatch, autoflush
):
    task_id = uuid.uuid4()
    task_fields = {
        "id": task_id,
        "project_id": uuid.uuid4(),
        "dataset_item_id": uuid.uuid4(),
        "display_id": f"T-{task_id.hex[:12]}",
        "file_name": "clip.mp4",
        "file_path": "clip.mp4",
        "file_type": "video",
        "status": "in_progress",
    }
    task = Task(**task_fields)
    make_transient_to_detached(task)
    session = AsyncSession(autoflush=autoflush)
    loaded = await session.merge(task, load=False)
    loaded.status = "local-edit"

    events = []
    original_expire = session.expire

    def expire(instance, attribute_names=None):
        events.append("expire")
        return original_expire(instance, attribute_names)

    async def flush():
        events.append("flush")
        assert not inspect(loaded).attrs.status.history.has_changes()

    async def read_core_row(db, row_model, row_id, *, populate_existing=False):
        events.append("read")
        assert row_model is labeling_core_read_relations.task_row
        assert row_id == task_id
        assert populate_existing is True
        refreshed = Task(**{**task_fields, "status": "owner-current"})
        make_transient_to_detached(refreshed)
        return await db.merge(refreshed, load=False)

    monkeypatch.setattr(session, "expire", expire)
    monkeypatch.setattr(session, "flush", flush)
    monkeypatch.setattr(control, "get_core_row", read_core_row)
    refreshed = await control.refresh_video_control_task(session, loaded)

    assert refreshed is loaded
    assert refreshed.status == "owner-current"
    assert events == (["expire", "flush", "read"] if autoflush else ["expire", "read"])

    async def missing_core_row(_db, _row_model, _row_id, *, populate_existing=False):
        assert populate_existing is True
        return None

    monkeypatch.setattr(control, "get_core_row", missing_core_row)
    with pytest.raises(InvalidRequestError, match="Could not refresh instance"):
        await control.refresh_video_control_task(session, loaded)
    await session.close()


@pytest.mark.asyncio
async def test_worker_segment_snapshot_binds_and_refreshes_segment_identity(
    monkeypatch,
):
    now = datetime.now(timezone.utc)
    segment_id = uuid.uuid4()
    item_id = uuid.uuid4()
    row = {
        "id": segment_id,
        "dataset_item_id": item_id,
        "segment_index": 2,
        "start_frame": 100,
        "end_frame": 199,
        "assignee_id": None,
        "status": "locked",
        "locked_by": uuid.uuid4(),
        "locked_at": now,
        "lock_expires_at": now,
        "created_at": now,
        "updated_at": now,
    }

    async def command(_db, request):
        assert isinstance(request, ReadWorkerSegment)
        return {"segment": dict(row), "segment_count": 4}

    monkeypatch.setattr(control, "video_control_command", command)
    session = AsyncSession()
    request = ReadWorkerSegment(
        schema="aap-labeling-video-control/v1",
        operation="read_worker_segment",
        job_id=uuid.uuid4(),
    )
    segment, count = await control.video_control_worker_segment(session, request)
    assert isinstance(segment, VideoSegment)
    assert inspect(segment).session is session.sync_session
    assert count == 4

    row["status"] = "completed"
    refreshed, refreshed_count = await control.video_control_worker_segment(
        session, request
    )
    assert refreshed is segment
    assert refreshed.status == "locked"
    assert refreshed_count == 4
    await session.close()


@pytest.mark.asyncio
async def test_worker_nowait_conflict_keeps_tracker_job_state_error(monkeypatch):
    async def locked(_db, request):
        assert isinstance(request, LockWorkerJob)
        raise control.VideoControlRejected(409, {"reason": "tracker_job_busy"})

    monkeypatch.setattr(runner, "video_control_job", locked)
    with pytest.raises(runner.TrackerJobStateConflict) as exc_info:
        await runner._load_job_for_update(AsyncSession(), uuid.uuid4())
    assert exc_info.value.detail["reason"] == "tracker_job_busy"
