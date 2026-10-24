"""3D Scene 跨帧任务中心 API 与 worker 合同。"""

from __future__ import annotations

import asyncio
import inspect
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.task import Task
from app.db.models.user import User
from app.services.cross_frame_job import JOB_KIND
from app.workers.cross_frame_job import (
    _finish_cancelled,
    _persist_progress,
    execute_cross_frame_job,
)
from tests.factory import create_project, create_user


def _install_jobs_queue(monkeypatch, callback=None):
    from app.services import jobs_queue

    requests = []

    async def dispatch_job(**request):
        requests.append(request)
        if callback is None:
            return request["execution_id"]
        result = callback(**request)
        if inspect.isawaitable(result):
            result = await result
        return result

    monkeypatch.setattr(jobs_queue, "dispatch_job", dispatch_job)
    return requests


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


async def _create_worker_job(db, *, owner_id, target_statuses):
    project, _, tasks = await _seed_scene(
        db, owner_id=owner_id, frames=range(len(target_statuses) + 1)
    )
    source = await _add_box(db, task=tasks[0], project=project, user_id=owner_id)
    for frame_index, status in enumerate(target_statuses, start=1):
        tasks[frame_index].status = status
    await db.flush()
    job = AsyncJob(
        kind=JOB_KIND,
        project_id=project.id,
        user_id=owner_id,
        status=AsyncJobStatus.PENDING.value,
        payload={
            "source_task_id": str(tasks[0].id),
            "sources": [
                {
                    "annotation_id": str(source.id),
                    "version": int(source.version or 1),
                }
            ],
            "targets": [
                {
                    "frame_index": frame_index,
                    "task_id": str(tasks[frame_index].id),
                    "preflight_state": "ready",
                }
                for frame_index in range(1, len(target_statuses) + 1)
            ],
        },
    )
    db.add(job)
    await db.flush()
    return job.id, tasks


async def test_create_cross_frame_job_is_explicit_and_singleflight(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    project, scene, tasks = await _seed_scene(db_session, owner_id=user.id)
    source = await _add_box(db_session, task=tasks[0], project=project, user_id=user.id)
    dispatched = []

    requests = _install_jobs_queue(
        monkeypatch,
        lambda **request: (
            dispatched.append(request["args"][0]) or request["execution_id"]
        ),
    )
    url = f"/api/v1/tasks/{tasks[0].id}/cross-frame-jobs"
    headers = {"Authorization": f"Bearer {token}"}
    first = await httpx_client.post(url, json=_request(source.id), headers=headers)
    second = await httpx_client.post(url, json=_request(source.id), headers=headers)

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json()["id"] == second.json()["id"]
    assert dispatched == [first.json()["id"]]
    assert requests[0]["task_name"] == (
        "app.workers.cross_frame_job.run_cross_frame_job"
    )
    assert requests[0]["args"] == [first.json()["id"]]
    stored_job = await db_session.get(
        AsyncJob, uuid.UUID(first.json()["id"]), populate_existing=True
    )
    assert requests[0]["execution_id"] == stored_job.celery_task_id
    assert "kwargs" not in requests[0]
    assert "options" not in requests[0]
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
    _install_jobs_queue(monkeypatch)
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
    _install_jobs_queue(monkeypatch)
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
    _install_jobs_queue(monkeypatch)
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
    _install_jobs_queue(monkeypatch)
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
    dispatches = _install_jobs_queue(monkeypatch)
    response = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/cross-frame-jobs",
        json=_request(uuid.uuid4(), start=1, end=1, scope="all"),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert dispatches == []


async def test_cross_frame_job_validates_scope_direction_and_editable_targets(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    project, _, tasks = await _seed_scene(db_session, owner_id=user.id, frames=[0, 1])
    source = await _add_box(db_session, task=tasks[0], project=project, user_id=user.id)
    _install_jobs_queue(monkeypatch)
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

    async def record_dispatch(**request):
        dispatched.append(request["args"][0])
        stored = await db_session.get(
            AsyncJob, uuid.UUID(request["args"][0]), populate_existing=True
        )
        assert stored.celery_task_id == request["execution_id"]
        return request["execution_id"]

    _install_jobs_queue(monkeypatch, record_dispatch)
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


async def test_cross_frame_worker_rechecks_role_after_frame_rollback(
    test_engine, monkeypatch
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as setup:
        owner = await create_user(
            setup,
            "super_admin",
            f"cross-frame-owner-{uuid.uuid4()}@worker.test",
            "Cross-frame owner",
        )
        job_id, _ = await _create_worker_job(
            setup, owner_id=owner.id, target_statuses=["completed", "in_progress"]
        )
        owner_id = owner.id

    first_failure_persisted = asyncio.Event()
    allow_next_frame = asyncio.Event()
    original_persist = execute_cross_frame_job.__globals__["_persist_progress"]
    persist_calls = 0

    async def pause_after_first_failure(db, **kwargs):
        nonlocal persist_calls
        await original_persist(db, **kwargs)
        if persist_calls == 0:
            persist_calls += 1
            first_failure_persisted.set()
            await allow_next_frame.wait()

    monkeypatch.setattr(
        "app.workers.cross_frame_job._persist_progress", pause_after_first_failure
    )

    async def run_worker():
        async with maker() as worker_db:
            await execute_cross_frame_job(worker_db, job_id=job_id)

    worker_task = asyncio.create_task(run_worker())
    try:
        await asyncio.wait_for(first_failure_persisted.wait(), timeout=5)
        async with maker.begin() as change_role:
            actor = await change_role.get(User, owner_id)
            assert actor is not None
            actor.role = "employee"
        allow_next_frame.set()
        await asyncio.wait_for(worker_task, timeout=10)
    finally:
        allow_next_frame.set()
        if not worker_task.done():
            worker_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker_task

    async with maker() as check:
        job = await check.get(AsyncJob, job_id)
        assert job is not None
        assert job.status == AsyncJobStatus.FAILED.value
        assert job.result["items"][0]["reason"] == "target_task_locked"
        assert job.result["items"][1]["reason"] == "permission_changed"


async def test_cross_frame_account_fence_holds_until_frame_commit(
    test_engine, monkeypatch
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as setup:
        owner = await create_user(
            setup,
            "super_admin",
            f"cross-frame-owner-{uuid.uuid4()}@worker.test",
            "Cross-frame owner",
        )
        job_id, _ = await _create_worker_job(
            setup, owner_id=owner.id, target_statuses=["in_progress"]
        )
        owner_id = owner.id

    frame_write_entered = asyncio.Event()
    allow_frame_write = asyncio.Event()
    disable_flush_started = asyncio.Event()
    disable_finished = asyncio.Event()

    class BlockingStager:
        async def stage_batch(self, **_kwargs):
            frame_write_entered.set()
            await allow_frame_write.wait()
            return 1

    monkeypatch.setattr(
        "app.workers.cross_frame_job.bind_cross_frame_batch_stager",
        lambda _db: BlockingStager(),
    )

    async def run_worker():
        async with maker() as worker_db:
            await execute_cross_frame_job(worker_db, job_id=job_id)

    async def disable_actor():
        async with maker() as disable_db:
            actor = await disable_db.get(User, owner_id)
            assert actor is not None
            actor.is_active = False
            disable_flush_started.set()
            await disable_db.flush()
            await disable_db.commit()
            disable_finished.set()

    worker_task = asyncio.create_task(run_worker())
    disable_task = None
    try:
        await asyncio.wait_for(frame_write_entered.wait(), timeout=5)
        disable_task = asyncio.create_task(disable_actor())
        await asyncio.wait_for(disable_flush_started.wait(), timeout=5)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(disable_task), timeout=0.05)
        allow_frame_write.set()
        await asyncio.wait_for(worker_task, timeout=10)
        await asyncio.wait_for(disable_task, timeout=10)
    finally:
        allow_frame_write.set()
        for task in (worker_task, disable_task):
            if task is not None and not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    async with maker() as check:
        job = await check.get(AsyncJob, job_id)
        actor = await check.get(User, owner_id)
        assert job is not None and actor is not None
        assert job.status == AsyncJobStatus.COMPLETED.value
        assert job.result["success_count"] == 1
        assert disable_finished.is_set()
        assert actor.is_active is False


async def test_cross_frame_missing_actor_after_rollback_keeps_generic_failure(
    db_session, super_admin, monkeypatch
):
    owner, _ = super_admin
    job_id, _ = await _create_worker_job(
        db_session,
        owner_id=owner.id,
        target_statuses=["completed", "in_progress", "in_progress"],
    )
    from app.services import project_write_guard

    lock_actor = project_write_guard.lock_actor_account_facts
    lock_calls = 0

    async def missing_on_second_frame(db, actor_id, *, nowait=True):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 2:
            return None
        return await lock_actor(db, actor_id, nowait=nowait)

    class SuccessfulStager:
        async def stage_batch(self, **_kwargs):
            return 1

    monkeypatch.setattr(
        project_write_guard, "lock_actor_account_facts", missing_on_second_frame
    )
    monkeypatch.setattr(
        "app.workers.cross_frame_job.bind_cross_frame_batch_stager",
        lambda _db: SuccessfulStager(),
    )

    await execute_cross_frame_job(db_session, job_id=job_id)
    job = await db_session.get(AsyncJob, job_id)
    await db_session.refresh(job)
    assert job.status == AsyncJobStatus.FAILED.value
    assert job.result["failed_count"] == 2
    assert job.result["success_count"] == 1
    assert [item["reason"] for item in job.result["items"]] == [
        "target_task_locked",
        "propagate_failed",
        None,
    ]


@pytest.mark.parametrize(
    "status,detail,reason",
    [
        (409, {"reason": "account_busy"}, "permission_changed"),
        (503, "身份认证服务暂不可用", "propagate_failed"),
    ],
    ids=["account-busy", "owner-unavailable"],
)
async def test_cross_frame_actor_lock_failure_preserves_error_category(
    db_session, super_admin, monkeypatch, status, detail, reason
):
    from fastapi import HTTPException

    owner, _ = super_admin
    job_id, _ = await _create_worker_job(
        db_session, owner_id=owner.id, target_statuses=["in_progress"]
    )

    async def unavailable(_db, _actor_id, *, nowait=True):
        raise HTTPException(status_code=status, detail=detail)

    monkeypatch.setattr(
        "app.services.project_write_guard.lock_actor_account_facts", unavailable
    )
    await execute_cross_frame_job(db_session, job_id=job_id)
    job = await db_session.get(AsyncJob, job_id)
    await db_session.refresh(job)
    assert job.status == AsyncJobStatus.FAILED.value
    assert job.result["failed_count"] == 1
    assert job.result["success_count"] == 0
    assert job.result["items"][0]["reason"] == reason
