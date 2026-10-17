"""3D Scene 跨帧任务中心 API 与 worker 合同。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.task import Task
from app.services.cross_frame_job import JOB_KIND
from app.workers.cross_frame_job import (
    _finish_cancelled,
    _persist_progress,
    execute_cross_frame_job,
)
from tests.factory import create_project


def _box3d():
    return {
        "type": "box_3d",
        "center": [1.0, 2.0, 3.0],
        "size": [4.0, 5.0, 6.0],
        "rotation": [0.0, 0.0, 0.0],
        "convention_at_create": "iso_8855",
    }


async def _seed_scene(db, *, owner_id, frames=range(4)):
    project = await create_project(db, owner_id=owner_id, type_key="lidar")
    project.data_type = "lidar"
    dataset = Dataset(
        display_id=f"DS-CF-{uuid.uuid4().hex[:6]}",
        name=f"cross-frame-{uuid.uuid4().hex[:6]}",
        data_type="point_cloud",
        created_by=owner_id,
    )
    db.add(dataset)
    await db.flush()
    scene = Scene(
        display_id=f"SCN-CF-{uuid.uuid4().hex[:6]}",
        dataset_id=dataset.id,
        name=f"scene-{uuid.uuid4().hex[:6]}",
    )
    db.add(scene)
    await db.flush()
    tasks = {}
    for frame_index in frames:
        item = DatasetItem(
            dataset_id=dataset.id,
            file_name=f"{frame_index:06d}.pcd",
            file_path=f"lidar/{frame_index:06d}.pcd",
            file_type="point_cloud",
            scene_id=scene.id,
            frame_index=frame_index,
        )
        db.add(item)
        await db.flush()
        task = Task(
            project_id=project.id,
            dataset_item_id=item.id,
            display_id=f"T-CF-{uuid.uuid4().hex[:8]}",
            file_name=item.file_name,
            file_path=item.file_path,
            file_type="point_cloud",
            status="in_progress",
        )
        db.add(task)
        await db.flush()
        tasks[frame_index] = task
    return project, scene, tasks


async def _add_box(db, *, task, project, user_id, track_id=None):
    row = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user_id,
        source="manual",
        annotation_type="box_3d",
        tool_unit_id="lidar_box_3d",
        class_name="car",
        geometry=_box3d(),
        track_id=track_id,
    )
    db.add(row)
    await db.flush()
    return row


def _request(annotation_id, *, start=1, end=2, scope="selected"):
    return {
        "operation": "propagate",
        "scope": scope,
        "annotation_ids": [str(annotation_id)] if scope == "selected" else [],
        "direction": "forward",
        "start_frame": start,
        "end_frame": end,
        "conflict_policy": "skip_existing",
    }


async def test_create_cross_frame_job_is_explicit_and_singleflight(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    project, scene, tasks = await _seed_scene(db_session, owner_id=user.id)
    source = await _add_box(db_session, task=tasks[0], project=project, user_id=user.id)
    dispatched = []

    def fake_delay(job_id):
        dispatched.append(job_id)
        return SimpleNamespace(id="celery-cross-frame")

    monkeypatch.setattr(
        "app.workers.cross_frame_job.run_cross_frame_job.delay", fake_delay
    )
    url = f"/api/v1/tasks/{tasks[0].id}/cross-frame-jobs"
    headers = {"Authorization": f"Bearer {token}"}
    first = await httpx_client.post(url, json=_request(source.id), headers=headers)
    second = await httpx_client.post(url, json=_request(source.id), headers=headers)

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json()["id"] == second.json()["id"]
    assert dispatched == [first.json()["id"]]
    payload = first.json()["payload"]
    assert payload["contract_version"] == 1
    assert payload["scene_id"] == str(scene.id)
    assert payload["scope"] == "selected"
    assert [target["frame_index"] for target in payload["targets"]] == [1, 2]
    assert all(target["preflight_state"] == "ready" for target in payload["targets"])


async def test_cross_frame_worker_propagates_range_and_skips_existing_track(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    project, _, tasks = await _seed_scene(db_session, owner_id=user.id)
    source = await _add_box(
        db_session,
        task=tasks[0],
        project=project,
        user_id=user.id,
        track_id="trk-cross-frame",
    )
    await _add_box(
        db_session,
        task=tasks[1],
        project=project,
        user_id=user.id,
        track_id="trk-cross-frame",
    )
    monkeypatch.setattr(
        "app.workers.cross_frame_job.run_cross_frame_job.delay",
        lambda _job_id: SimpleNamespace(id="celery-cross-frame"),
    )
    response = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/cross-frame-jobs",
        json=_request(source.id),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 202, response.text
    job_id = uuid.UUID(response.json()["id"])

    await execute_cross_frame_job(db_session, job_id=job_id, celery_task_id="worker-1")

    job = await db_session.get(AsyncJob, job_id)
    await db_session.refresh(job)
    assert job.status == "completed"
    assert job.progress_pct == 100
    assert job.result["success_count"] == 1
    assert job.result["skipped_count"] == 1
    assert job.result["created_annotation_count"] == 1
    target_tracks = list(
        (
            await db_session.execute(
                select(Annotation.track_id)
                .where(Annotation.task_id == tasks[2].id)
                .where(Annotation.is_active.is_(True))
            )
        ).scalars()
    )
    assert target_tracks == ["trk-cross-frame"]


async def test_cross_frame_worker_marks_changed_source_stale(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    project, _, tasks = await _seed_scene(db_session, owner_id=user.id)
    source = await _add_box(db_session, task=tasks[0], project=project, user_id=user.id)
    monkeypatch.setattr(
        "app.workers.cross_frame_job.run_cross_frame_job.delay",
        lambda _job_id: SimpleNamespace(id="celery-cross-frame"),
    )
    response = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/cross-frame-jobs",
        json=_request(source.id),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 202
    source.version += 1
    await db_session.commit()

    job_id = uuid.UUID(response.json()["id"])
    await execute_cross_frame_job(db_session, job_id=job_id)
    job = await db_session.get(AsyncJob, job_id)
    await db_session.refresh(job)
    assert job.status == "failed"
    assert job.result["stale_count"] == 2
    assert job.result["created_annotation_count"] == 0


async def test_cross_frame_progress_replaces_result_and_persists_progress(
    db_session, super_admin
):
    user, _ = super_admin
    job = AsyncJob(
        kind=JOB_KIND,
        user_id=user.id,
        status=AsyncJobStatus.RUNNING.value,
        result={"obsolete_key": True},
    )
    db_session.add(job)
    await db_session.flush()

    items = [
        {"status": "success", "created_count": 2},
        {"status": "skipped", "created_count": 0},
    ]
    await _persist_progress(
        db_session,
        job_id=job.id,
        ordered_items=items,
        total=4,
    )

    await db_session.refresh(job)
    assert job.progress_pct == 50
    assert job.result == {
        "contract_version": 1,
        "total_count": 2,
        "success_count": 1,
        "skipped_count": 1,
        "failed_count": 0,
        "stale_count": 0,
        "cancelled_count": 0,
        "created_annotation_count": 2,
        "items": items,
    }


@pytest.mark.parametrize(
    "status", [AsyncJobStatus.COMPLETED.value, AsyncJobStatus.FAILED.value]
)
async def test_cross_frame_cancel_writer_overwrites_terminal_job(
    db_session, super_admin, status
):
    user, _ = super_admin
    job = AsyncJob(
        kind=JOB_KIND,
        user_id=user.id,
        status=status,
        progress_pct=35,
        result={"previous": True},
        error_message="earlier execution issue",
    )
    db_session.add(job)
    await db_session.flush()

    result = {"cancelled_count": 2, "items": []}
    await _finish_cancelled(db_session, job_id=job.id, result=result)

    await db_session.refresh(job)
    assert job.status == AsyncJobStatus.CANCELLED.value
    assert job.progress_pct == 100
    assert job.result == result
    assert job.error_message == "earlier execution issue"


async def test_cross_frame_cancel_is_immediate_when_pending_and_cooperative_when_running(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    project, _, tasks = await _seed_scene(db_session, owner_id=user.id)
    source = await _add_box(db_session, task=tasks[0], project=project, user_id=user.id)
    monkeypatch.setattr(
        "app.workers.cross_frame_job.run_cross_frame_job.delay",
        lambda _job_id: SimpleNamespace(id="celery-cross-frame"),
    )
    monkeypatch.setattr(
        "app.workers.celery_app.celery_app.control.revoke",
        lambda *_args, **_kwargs: None,
    )
    headers = {"Authorization": f"Bearer {token}"}
    create_url = f"/api/v1/tasks/{tasks[0].id}/cross-frame-jobs"

    pending_response = await httpx_client.post(
        create_url, json=_request(source.id), headers=headers
    )
    pending_job_id = pending_response.json()["id"]
    pending_cancel = await httpx_client.post(
        f"/api/v1/async-jobs/{pending_job_id}/cancel", headers=headers
    )
    assert pending_cancel.json()["status"] == "cancelled"
    pending_job = await db_session.get(AsyncJob, uuid.UUID(pending_job_id))
    await db_session.refresh(pending_job)
    assert pending_job.status == "cancelled"
    assert pending_job.result["cancelled_count"] == 2
    await execute_cross_frame_job(db_session, job_id=pending_job.id)
    created_after_cancel = await db_session.scalar(
        select(func.count(Annotation.id)).where(
            Annotation.task_id.in_([tasks[1].id, tasks[2].id])
        )
    )
    assert created_after_cancel == 0

    running_response = await httpx_client.post(
        create_url, json=_request(source.id), headers=headers
    )
    running_job = await db_session.get(
        AsyncJob, uuid.UUID(running_response.json()["id"])
    )
    running_job.status = "running"
    await db_session.commit()
    running_cancel = await httpx_client.post(
        f"/api/v1/async-jobs/{running_job.id}/cancel", headers=headers
    )
    assert running_cancel.json()["status"] == "cancel_requested"
    await db_session.refresh(running_job)
    assert running_job.status == "running"
    assert running_job.payload["cancel_requested"] is True


async def test_cross_frame_worker_continues_after_one_target_rolls_back(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    project, _, tasks = await _seed_scene(db_session, owner_id=user.id)
    source = await _add_box(db_session, task=tasks[0], project=project, user_id=user.id)
    monkeypatch.setattr(
        "app.workers.cross_frame_job.run_cross_frame_job.delay",
        lambda _job_id: SimpleNamespace(id="celery-cross-frame"),
    )
    response = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/cross-frame-jobs",
        json=_request(source.id),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 202, response.text
    tasks[1].status = "completed"
    await db_session.commit()

    job_id = uuid.UUID(response.json()["id"])
    await execute_cross_frame_job(db_session, job_id=job_id)

    job = await db_session.get(AsyncJob, job_id)
    await db_session.refresh(job)
    assert job.status == "failed"
    assert job.result["failed_count"] == 1
    assert job.result["success_count"] == 1
    assert job.result["items"][0]["reason"] == "target_task_locked"
    created_on_second_target = await db_session.scalar(
        select(func.count(Annotation.id)).where(Annotation.task_id == tasks[2].id)
    )
    assert created_on_second_target == 1


async def test_cross_frame_all_scope_caps_source_snapshot(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    project, _, tasks = await _seed_scene(db_session, owner_id=user.id, frames=[0, 1])
    for _ in range(501):
        await _add_box(
            db_session,
            task=tasks[0],
            project=project,
            user_id=user.id,
        )
    dispatched = False

    def fake_delay(_job_id):
        nonlocal dispatched
        dispatched = True
        return SimpleNamespace(id="unused")

    monkeypatch.setattr(
        "app.workers.cross_frame_job.run_cross_frame_job.delay", fake_delay
    )
    response = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/cross-frame-jobs",
        json=_request(uuid.uuid4(), start=1, end=1, scope="all"),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert dispatched is False


async def test_cross_frame_job_validates_scope_direction_and_editable_targets(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    project, _, tasks = await _seed_scene(db_session, owner_id=user.id, frames=[0, 1])
    source = await _add_box(db_session, task=tasks[0], project=project, user_id=user.id)
    monkeypatch.setattr(
        "app.workers.cross_frame_job.run_cross_frame_job.delay",
        lambda _job_id: SimpleNamespace(id="unused"),
    )
    url = f"/api/v1/tasks/{tasks[0].id}/cross-frame-jobs"
    headers = {"Authorization": f"Bearer {token}"}

    wrong_scope = _request(source.id, scope="all")
    wrong_scope["annotation_ids"] = [str(source.id)]
    wrong_direction = _request(source.id, start=0, end=1)
    tasks[1].status = "completed"
    await db_session.flush()

    assert (
        await httpx_client.post(url, json=wrong_scope, headers=headers)
    ).status_code == 422
    assert (
        await httpx_client.post(url, json=wrong_direction, headers=headers)
    ).status_code == 422
    no_target = await httpx_client.post(
        url, json=_request(source.id, start=1, end=1), headers=headers
    )
    assert no_target.status_code == 409


async def test_retry_failed_cross_frame_job_refreshes_source_snapshot(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    project, _, tasks = await _seed_scene(db_session, owner_id=user.id)
    source = await _add_box(db_session, task=tasks[0], project=project, user_id=user.id)
    dispatched = []
    monkeypatch.setattr(
        "app.workers.cross_frame_job.run_cross_frame_job.delay",
        lambda job_id: (
            dispatched.append(job_id) or SimpleNamespace(id=f"celery-{len(dispatched)}")
        ),
    )
    response = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/cross-frame-jobs",
        json=_request(source.id),
        headers={"Authorization": f"Bearer {token}"},
    )
    job = await db_session.get(AsyncJob, uuid.UUID(response.json()["id"]))
    job.status = "failed"
    job.result = {
        "items": [
            {
                "frame_index": 2,
                "task_id": str(tasks[2].id),
                "status": "stale",
            }
        ]
    }
    source.version += 1
    await db_session.commit()

    retry = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/cross-frame-jobs/{job.id}/retry-failed",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert retry.status_code == 202, retry.text
    retry_payload = retry.json()["payload"]
    assert retry_payload["parent_job_id"] == str(job.id)
    assert [target["frame_index"] for target in retry_payload["targets"]] == [2]
    assert retry_payload["sources"][0]["version"] == source.version
    assert len(dispatched) == 2
