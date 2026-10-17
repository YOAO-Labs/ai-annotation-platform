"""Durable idempotency for task annotation creation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.annotation_operation import AnnotationOperation
from app.db.models.audit_log import AuditLog
from app.services.task_lock import TaskLockService
from tests.factory import create_project, create_task, create_user


def _bearer(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def _payload() -> dict:
    return {
        "annotation_type": "bbox",
        "class_name": "car",
        "geometry": {"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
    }


async def _seed(db_session, super_admin):
    user, token = super_admin
    project = await create_project(db_session, owner_id=user.id)
    task = await create_task(db_session, project_id=project.id, status="in_progress")
    return task, token


@pytest.mark.asyncio
async def test_create_annotation_replays_durable_receipt(
    httpx_client, db_session, super_admin
):
    task, token = await _seed(db_session, super_admin)
    key = f"offline-create-{uuid.uuid4()}"
    headers = _bearer(token, **{"Idempotency-Key": key})

    first = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_payload(),
        headers=headers,
    )
    second = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_payload(),
        headers=headers,
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json() == first.json()

    count = await db_session.scalar(
        select(func.count())
        .select_from(Annotation)
        .where(Annotation.task_id == task.id)
    )
    assert count == 1
    operation = await db_session.scalar(
        select(AnnotationOperation).where(
            AnnotationOperation.task_id == task.id,
            AnnotationOperation.actor_id == super_admin[0].id,
            AnnotationOperation.idempotency_key == key,
        )
    )
    assert operation is not None
    assert operation.kind == "create_annotation"
    assert operation.response_json == first.json()


@pytest.mark.asyncio
async def test_create_annotation_rejects_key_reuse_with_changed_request(
    httpx_client, db_session, super_admin
):
    task, token = await _seed(db_session, super_admin)
    key = f"offline-create-{uuid.uuid4()}"
    headers = _bearer(token, **{"Idempotency-Key": key})

    first = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_payload(),
        headers=headers,
    )
    changed = {**_payload(), "class_name": "person"}
    conflict = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=changed,
        headers=headers,
    )

    assert first.status_code == 201, first.text
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["reason"] == "idempotency_conflict"
    operation = await db_session.scalar(
        select(AnnotationOperation).where(
            AnnotationOperation.task_id == task.id,
            AnnotationOperation.actor_id == super_admin[0].id,
            AnnotationOperation.idempotency_key == key,
        )
    )
    assert operation is not None
    assert conflict.json()["detail"]["operation_id"] == str(operation.id)
    count = await db_session.scalar(
        select(func.count())
        .select_from(Annotation)
        .where(Annotation.task_id == task.id)
    )
    assert count == 1


@pytest.mark.asyncio
async def test_create_annotation_without_key_keeps_legacy_behavior(
    httpx_client, db_session, super_admin
):
    task, token = await _seed(db_session, super_admin)
    headers = _bearer(token)

    first = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_payload(),
        headers=headers,
    )
    second = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_payload(),
        headers=headers,
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json()["id"] != first.json()["id"]
    count = await db_session.scalar(
        select(func.count())
        .select_from(Annotation)
        .where(Annotation.task_id == task.id)
    )
    assert count == 2


@pytest.mark.asyncio
async def test_create_annotation_rejects_invalid_idempotency_key(
    httpx_client, db_session, super_admin
):
    task, token = await _seed(db_session, super_admin)
    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_payload(),
        headers=_bearer(token, **{"Idempotency-Key": " "}),
    )

    assert response.status_code == 400, response.text
    count = await db_session.scalar(
        select(func.count())
        .select_from(Annotation)
        .where(Annotation.task_id == task.id)
    )
    assert count == 0


@pytest.mark.asyncio
async def test_create_annotation_replays_frozen_response_without_write_side_effects(
    httpx_client, db_session, super_admin
):
    task, token = await _seed(db_session, super_admin)
    user, _ = super_admin
    key = f"historical-create-{uuid.uuid4()}"
    headers = _bearer(token, **{"Idempotency-Key": key})
    lock = await TaskLockService(db_session).acquire(task.id, user.id)

    first = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_payload(),
        headers=headers,
    )
    assert first.status_code == 201, first.text
    original = first.json()
    annotation_id = original["id"]

    edited = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/annotations/{annotation_id}",
        json={"class_name": "person"},
        headers=_bearer(token),
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["class_name"] == "person"

    task.annotation_contributor_ids = []
    lock.expire_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    await db_session.flush()
    history_before = (
        await httpx_client.get(
            f"/api/v1/tasks/{task.id}/audit-history",
            headers=_bearer(token),
        )
    ).json()["entries"]
    annotation_count_before = await db_session.scalar(
        select(func.count())
        .select_from(Annotation)
        .where(Annotation.task_id == task.id)
    )
    operation_count_before = await db_session.scalar(
        select(func.count())
        .select_from(AnnotationOperation)
        .where(AnnotationOperation.task_id == task.id)
    )
    audit_count_before = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.target_id == annotation_id)
    )
    contributor_ids_before = list(task.annotation_contributor_ids)
    heartbeat_before = lock.expire_at

    replay = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_payload(),
        headers=headers,
    )

    assert replay.status_code == 201, replay.text
    assert replay.json() == original
    stored_annotation = await db_session.get(Annotation, uuid.UUID(annotation_id))
    assert stored_annotation.class_name == "person"
    assert task.annotation_contributor_ids == contributor_ids_before == []
    assert lock.expire_at == heartbeat_before
    assert (
        (
            await db_session.scalar(
                select(func.count())
                .select_from(Annotation)
                .where(Annotation.task_id == task.id)
            )
        )
        == annotation_count_before
        == 1
    )
    assert (
        (
            await db_session.scalar(
                select(func.count())
                .select_from(AnnotationOperation)
                .where(AnnotationOperation.task_id == task.id)
            )
        )
        == operation_count_before
        == 1
    )
    assert (
        (
            await db_session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.target_id == annotation_id)
            )
        )
        == audit_count_before
        == 2
    )
    history_after = (
        await httpx_client.get(
            f"/api/v1/tasks/{task.id}/audit-history",
            headers=_bearer(token),
        )
    ).json()["entries"]
    assert history_after == history_before


@pytest.mark.asyncio
async def test_create_annotation_key_scope_is_actor_task_and_key(
    httpx_client, db_session, super_admin
):
    task, token = await _seed(db_session, super_admin)
    second_task = await create_task(
        db_session, project_id=task.project_id, status="in_progress"
    )
    second_actor = await create_user(
        db_session,
        "super_admin",
        f"admin-{uuid.uuid4()}@test.local",
        "Second Admin",
    )
    second_token = create_access_token(
        subject=str(second_actor.id), role=second_actor.role
    )
    key = f"scoped-create-{uuid.uuid4()}"

    responses = [
        await httpx_client.post(
            f"/api/v1/tasks/{task.id}/annotations",
            json=_payload(),
            headers=_bearer(token, **{"Idempotency-Key": key}),
        ),
        await httpx_client.post(
            f"/api/v1/tasks/{task.id}/annotations",
            json=_payload(),
            headers=_bearer(second_token, **{"Idempotency-Key": key}),
        ),
        await httpx_client.post(
            f"/api/v1/tasks/{second_task.id}/annotations",
            json=_payload(),
            headers=_bearer(token, **{"Idempotency-Key": key}),
        ),
        await httpx_client.post(
            f"/api/v1/tasks/{task.id}/annotations",
            json=_payload(),
            headers=_bearer(token, **{"Idempotency-Key": f"{key}-other"}),
        ),
    ]

    assert [response.status_code for response in responses] == [201] * 4
    assert len({response.json()["id"] for response in responses}) == 4
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AnnotationOperation)
            .where(AnnotationOperation.idempotency_key.in_([key, f"{key}-other"]))
        )
        == 4
    )


@pytest.mark.asyncio
async def test_create_annotation_trims_key_and_accepts_128_characters(
    httpx_client, db_session, super_admin
):
    task, token = await _seed(db_session, super_admin)
    trimmed = f"trimmed-{uuid.uuid4()}"
    headers = _bearer(token, **{"Idempotency-Key": f"  {trimmed}  "})

    first = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_payload(),
        headers=headers,
    )
    replay = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json={
            "geometry": _payload()["geometry"],
            "class_name": "car",
            "annotation_type": "bbox",
        },
        headers=_bearer(token, **{"Idempotency-Key": trimmed}),
    )
    maximum = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_payload(),
        headers=_bearer(token, **{"Idempotency-Key": "k" * 128}),
    )
    too_long = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_payload(),
        headers=_bearer(token, **{"Idempotency-Key": "k" * 129}),
    )

    assert first.status_code == replay.status_code == maximum.status_code == 201
    assert replay.json() == first.json()
    assert too_long.status_code == 400


@pytest.mark.asyncio
async def test_create_annotation_rejects_non_create_operation_key_reuse(
    httpx_client, db_session, super_admin
):
    task, token = await _seed(db_session, super_admin)
    operation = AnnotationOperation(
        task_id=task.id,
        actor_id=super_admin[0].id,
        kind="slice_polygon",
        idempotency_key=f"other-operation-{uuid.uuid4()}",
        request_digest="0" * 64,
        scope_fingerprint="0" * 64,
        source_versions={},
        result_versions={},
        report={},
        response_json={},
    )
    db_session.add(operation)
    await db_session.flush()

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_payload(),
        headers=_bearer(token, **{"Idempotency-Key": operation.idempotency_key}),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "idempotency_conflict"
    assert response.json()["detail"]["operation_id"] == str(operation.id)
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Annotation)
            .where(Annotation.task_id == task.id)
        )
        == 0
    )
