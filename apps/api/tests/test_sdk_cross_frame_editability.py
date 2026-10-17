"""Cross-frame API edits must check source task editability before side effects."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.db.models.task_lock import TaskLock
from app.services.annotation import AnnotationService
from app.services.task_lock import TaskLockService
from tests.factory import create_membership
from tests.test_annotation_propagate import _add_annotation, _box3d, _seed_scene


_OPERATIONS = (
    ("propagate", "propagate"),
    ("propagate_batch", "propagate_batch"),
    ("interpolate_range", "interpolate_range"),
)


def _request_path_and_body(operation, source_task, source_annotation, target_task):
    if operation == "propagate":
        return (
            f"/api/v1/tasks/{source_task.id}/annotations/"
            f"{source_annotation.id}/propagate-to-task",
            {"target_task_id": str(target_task.id)},
        )
    if operation == "propagate_batch":
        return (
            f"/api/v1/tasks/{source_task.id}/annotations/propagate-batch",
            {"target_task_id": str(target_task.id)},
        )
    return (
        f"/api/v1/tasks/{source_task.id}/annotations/interpolate-range",
        {"track_id": source_annotation.track_id, "to_task_id": str(target_task.id)},
    )


async def _annotation_snapshot(db_session, tasks):
    rows = (
        await db_session.execute(
            select(
                Annotation.id,
                Annotation.task_id,
                Annotation.version,
                Annotation.track_id,
            ).where(Annotation.task_id.in_([task.id for task in tasks]))
        )
    ).all()
    return {
        (annotation_id, task_id): (version, track_id)
        for annotation_id, task_id, version, track_id in rows
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("operation", "service_method"), _OPERATIONS)
async def test_completed_source_rejects_before_cross_frame_side_effects(
    operation,
    service_method,
    db_session,
    httpx_client,
    super_admin,
    monkeypatch,
):
    user, token = super_admin
    project, _, _, tasks = await _seed_scene(db_session, owner_id=user.id, n=3)
    source_task = tasks[0]
    target_task = tasks[2] if operation == "interpolate_range" else tasks[1]
    track_id = "trk_sdk_interpolation" if operation == "interpolate_range" else None
    source_annotation = await _add_annotation(
        db_session,
        task=source_task,
        project=project,
        user_id=user.id,
        geometry=_box3d(),
        track_id=track_id,
    )
    if operation == "interpolate_range":
        await _add_annotation(
            db_session,
            task=target_task,
            project=project,
            user_id=user.id,
            geometry=_box3d(center=(7.0, 2.0, 3.0)),
            track_id=track_id,
        )
    source_task.status = "completed"
    await db_session.flush()

    before = await _annotation_snapshot(db_session, tasks)
    source_version = source_annotation.version
    source_track_id = source_annotation.track_id
    service_calls: list[str] = []
    heartbeat_calls: list[tuple[object, ...]] = []

    async def forbidden_service_call(self, *args, **kwargs):
        service_calls.append(service_method)
        raise AssertionError(
            f"{service_method} ran before source editability was checked"
        )

    async def record_heartbeat(self, *args, **kwargs):
        heartbeat_calls.append(args)
        return False

    monkeypatch.setattr(AnnotationService, service_method, forbidden_service_call)
    monkeypatch.setattr(TaskLockService, "heartbeat", record_heartbeat)

    path, body = _request_path_and_body(
        operation, source_task, source_annotation, target_task
    )
    response = await httpx_client.post(
        path,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "task_locked"
    assert response.json()["detail"]["status"] == "completed"
    assert service_calls == []
    assert heartbeat_calls == []

    await db_session.refresh(source_task)
    await db_session.refresh(source_annotation)
    assert source_task.status == "completed"
    assert source_annotation.version == source_version
    assert source_annotation.track_id == source_track_id
    assert await _annotation_snapshot(db_session, tasks) == before

    remaining_locks = (
        await db_session.execute(
            select(TaskLock.id).where(TaskLock.task_id.in_([task.id for task in tasks]))
        )
    ).all()
    assert remaining_locks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [operation for operation, _ in _OPERATIONS])
async def test_editable_source_still_supports_each_cross_frame_route(
    operation,
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _, tasks = await _seed_scene(db_session, owner_id=user.id, n=3)
    source_task = tasks[0]
    target_task = tasks[2] if operation == "interpolate_range" else tasks[1]
    track_id = "trk_sdk_interpolation" if operation == "interpolate_range" else None
    source_annotation = await _add_annotation(
        db_session,
        task=source_task,
        project=project,
        user_id=user.id,
        geometry=_box3d(),
        track_id=track_id,
    )
    if operation == "interpolate_range":
        await _add_annotation(
            db_session,
            task=target_task,
            project=project,
            user_id=user.id,
            geometry=_box3d(center=(7.0, 2.0, 3.0)),
            track_id=track_id,
        )
    await db_session.flush()

    path, body = _request_path_and_body(
        operation, source_task, source_annotation, target_task
    )
    response = await httpx_client.post(
        path,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_reviewer_can_still_adjust_review_tasks_during_propagation(
    db_session,
    httpx_client,
    super_admin,
    reviewer,
):
    owner, owner_token = super_admin
    reviewer_user, reviewer_token = reviewer
    project, _, _, tasks = await _seed_scene(db_session, owner_id=owner.id, n=3)
    source_task, target_task = tasks[:2]
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=reviewer_user.id,
        role="reviewer",
    )
    source_task.assignee_id = owner.id
    target_task.assignee_id = owner.id
    source_annotation = await _add_annotation(
        db_session,
        task=source_task,
        project=project,
        user_id=owner.id,
        geometry=_box3d(),
        track_id="trk_reviewer_edit",
    )
    await _add_annotation(
        db_session,
        task=target_task,
        project=project,
        user_id=owner.id,
        geometry=_box3d(center=(7.0, 2.0, 3.0)),
        track_id="trk_other",
    )
    await db_session.flush()

    for task in (source_task, target_task):
        submitted = await httpx_client.post(
            f"/api/v1/tasks/{task.id}/submit",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert submitted.status_code == 200, submitted.text
        task.reviewer_id = reviewer_user.id
    await db_session.flush()

    response = await httpx_client.post(
        f"/api/v1/tasks/{source_task.id}/annotations/{source_annotation.id}/propagate-to-task",
        json={"target_task_id": str(target_task.id)},
        headers={"Authorization": f"Bearer {reviewer_token}"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["annotation"]["task_id"] == str(target_task.id)
