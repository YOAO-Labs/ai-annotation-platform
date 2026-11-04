from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.db.models.annotation_operation import AnnotationOperation
from app.db.models.prediction import Prediction
from tests.factory import create_membership, create_project, create_task


def _headers(token, key=None):
    result = {"Authorization": f"Bearer {token}"}
    if key:
        result["Idempotency-Key"] = key
    return result


async def test_acceptance_history_is_atomic_idempotent_and_task_actor_fenced(
    db_session, httpx_client, super_admin, annotator, reviewer
):
    owner, _ = super_admin
    actor, token = annotator
    other, other_token = reviewer
    project = await create_project(
        db_session, owner_id=owner.id, classes=["car", "person"]
    )
    membership = await create_membership(
        db_session, project_id=project.id, user_id=actor.id, role="annotator"
    )
    await create_membership(
        db_session, project_id=project.id, user_id=other.id, role="reviewer"
    )
    task = await create_task(db_session, project_id=project.id)
    foreign = await create_task(db_session, project_id=project.id)
    task.assignee_id = actor.id
    prediction = Prediction(
        task_id=task.id,
        project_id=project.id,
        result=[
            {
                "type": "rectanglelabels",
                "value": {
                    "x": x,
                    "y": 0,
                    "width": 10,
                    "height": 10,
                    "rectanglelabels": ["car"],
                },
            }
            for x in (0, 20)
        ],
    )
    db_session.add(prediction)
    await db_session.commit()
    task_id, prediction_id = task.id, prediction.id
    key = str(uuid.uuid4())
    accept_url = f"/api/v1/tasks/{task_id}/predictions/{prediction_id}/accept"
    response = await httpx_client.post(
        accept_url,
        params={"override_class_name": "person"},
        json={"attribute_overrides": {"weather": "rain"}},
        headers=_headers(token, key),
    )
    assert response.status_code == 200, response.text
    accepted = response.json()
    assert len(accepted) == 2
    assert all(
        row["class_name"] == "person" and row["attributes"]["weather"] == "rain"
        for row in accepted
    )
    replay = await httpx_client.post(
        accept_url,
        params={"override_class_name": "person"},
        json={"attribute_overrides": {"weather": "rain"}},
        headers=_headers(token, key),
    )
    assert replay.status_code == 200 and replay.json() == accepted
    conflict = await httpx_client.post(
        accept_url, params={"override_class_name": "car"}, headers=_headers(token, key)
    )
    assert conflict.status_code == 409
    ids = [row["id"] for row in accepted]
    versions = {row["id"]: row["version"] for row in accepted}
    url = f"/api/v1/tasks/{task_id}/acceptance-history/{key}/restore"
    transition = {
        "target": "before",
        "expected_versions": versions,
        "idempotency_key": str(uuid.uuid4()),
        "video_segment_id": None,
    }
    wrong_task = await httpx_client.post(
        f"/api/v1/tasks/{foreign.id}/acceptance-history/{key}/restore",
        json=transition,
        headers=_headers(token),
    )
    assert wrong_task.status_code in (403, 404)
    wrong_actor = await httpx_client.post(
        url, json=transition, headers=_headers(other_token)
    )
    assert wrong_actor.status_code in (403, 404)
    undo = await httpx_client.post(url, json=transition, headers=_headers(token))
    assert undo.status_code == 200, undo.text
    undone = undo.json()
    assert undone["active_annotation_ids"] == []
    lost_response_retry = await httpx_client.post(
        url, json=transition, headers=_headers(token)
    )
    assert lost_response_retry.status_code == 200
    assert lost_response_retry.json()["result_versions"] == undone["result_versions"]
    assert lost_response_retry.json()["idempotent_replay"] is True
    stale = await httpx_client.post(
        url,
        json={**transition, "target": "after", "idempotency_key": str(uuid.uuid4())},
        headers=_headers(token),
    )
    assert stale.status_code == 409
    redo = await httpx_client.post(
        url,
        json={
            "target": "after",
            "expected_versions": undone["result_versions"],
            "idempotency_key": str(uuid.uuid4()),
            "video_segment_id": None,
        },
        headers=_headers(token),
    )
    assert redo.status_code == 200, redo.text
    assert set(redo.json()["active_annotation_ids"]) == set(ids)
    second_undo = await httpx_client.post(
        url,
        json={
            "target": "before",
            "expected_versions": redo.json()["result_versions"],
            "idempotency_key": str(uuid.uuid4()),
            "video_segment_id": None,
        },
        headers=_headers(token),
    )
    assert second_undo.status_code == 200, second_undo.text
    redo = await httpx_client.post(
        url,
        json={
            "target": "after",
            "expected_versions": second_undo.json()["result_versions"],
            "idempotency_key": str(uuid.uuid4()),
            "video_segment_id": None,
        },
        headers=_headers(token),
    )
    assert redo.status_code == 200, redo.text
    rows = (
        await db_session.scalars(
            select(Annotation).where(Annotation.task_id == task_id)
        )
    ).all()
    assert {str(row.id) for row in rows} == set(ids)
    assert all(
        row.is_active
        and row.parent_prediction_id == prediction_id
        and row.class_name == "person"
        and row.attributes["weather"] == "rain"
        for row in rows
    )
    await db_session.refresh(task)
    task.media_source_generation += 1
    await db_session.commit()
    changed_source = await httpx_client.post(
        url,
        json={
            "target": "before",
            "expected_versions": redo.json()["result_versions"],
            "idempotency_key": str(uuid.uuid4()),
            "video_segment_id": None,
        },
        headers=_headers(token),
    )
    assert changed_source.status_code == 409
    operation = await db_session.scalar(
        select(AnnotationOperation).where(
            AnnotationOperation.task_id == task_id,
            AnnotationOperation.kind == "accept_prediction",
        )
    )
    operation.created_at = datetime.now(timezone.utc) - timedelta(days=31)
    await db_session.commit()
    expired = await httpx_client.post(
        url,
        json={
            "target": "before",
            "expected_versions": redo.json()["result_versions"],
            "idempotency_key": str(uuid.uuid4()),
            "video_segment_id": None,
        },
        headers=_headers(token),
    )
    assert expired.status_code == 410
    await db_session.delete(membership)
    await db_session.commit()
    revoked = await httpx_client.post(
        url,
        json={
            "target": "before",
            "expected_versions": redo.json()["result_versions"],
            "idempotency_key": str(uuid.uuid4()),
            "video_segment_id": None,
        },
        headers=_headers(token),
    )
    assert revoked.status_code in (403, 404)


async def test_multi_annotation_restore_rolls_back_if_one_row_changed(
    db_session, httpx_client, super_admin
):
    actor, token = super_admin
    project = await create_project(
        db_session, owner_id=actor.id, classes=["car", "person"]
    )
    task = await create_task(db_session, project_id=project.id)
    prediction = Prediction(
        task_id=task.id,
        project_id=project.id,
        result=[
            {
                "type": "rectanglelabels",
                "value": {
                    "x": x,
                    "y": 0,
                    "width": 10,
                    "height": 10,
                    "rectanglelabels": ["car"],
                },
            }
            for x in (0, 20)
        ],
    )
    db_session.add(prediction)
    await db_session.commit()
    task_id = task.id
    key = str(uuid.uuid4())
    accepted = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/predictions/{prediction.id}/accept",
        headers=_headers(token, key),
    )
    assert accepted.status_code == 200, accepted.text
    rows = accepted.json()
    changed_id = max(row["id"] for row in rows)
    changed = await db_session.get(Annotation, uuid.UUID(changed_id))
    changed.class_name = "person"
    changed.version += 1
    await db_session.commit()
    restore = await httpx_client.post(
        f"/api/v1/tasks/{task_id}/acceptance-history/{key}/restore",
        headers=_headers(token),
        json={
            "target": "before",
            "expected_versions": {r["id"]: r["version"] for r in rows},
            "idempotency_key": str(uuid.uuid4()),
            "video_segment_id": None,
        },
    )
    assert restore.status_code == 409
    persisted = (
        await db_session.scalars(
            select(Annotation)
            .where(Annotation.task_id == task_id)
            .execution_options(populate_existing=True)
        )
    ).all()
    assert len(persisted) == 2
    assert all(row.is_active for row in persisted)
    assert (
        next(row for row in persisted if str(row.id) == changed_id).class_name
        == "person"
    )


@pytest.mark.parametrize("media_type", ["image", "video"])
async def test_restore_rejects_real_editor_takeover_before_undo_and_redo(
    media_type, db_session, httpx_client, super_admin, reviewer
):
    from app.services.task_lock import TaskLockService

    actor, token = super_admin
    other, _ = reviewer
    project = await create_project(db_session, owner_id=actor.id, classes=["car"])
    task = await create_task(db_session, project_id=project.id)
    task.file_type = media_type
    prediction = Prediction(
        task_id=task.id,
        project_id=project.id,
        source="external_import",
        result=[
            {
                "type": "rectanglelabels",
                "value": {
                    "x": 0,
                    "y": 0,
                    "width": 10,
                    "height": 10,
                    "rectanglelabels": ["car"],
                },
            }
        ],
    )
    db_session.add(prediction)
    await db_session.commit()
    task_id, actor_id, other_id = task.id, actor.id, other.id
    key = str(uuid.uuid4())
    accepted = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/predictions/{prediction.id}/accept",
        headers=_headers(token, key),
    )
    assert accepted.status_code == 200, accepted.text
    annotation = accepted.json()[0]
    locks = TaskLockService(db_session)
    assert await locks.acquire(task_id, actor_id) is not None
    assert await locks.acquire(task_id, other_id, force_takeover=True) is not None
    await db_session.commit()
    url = f"/api/v1/tasks/{task_id}/acceptance-history/{key}/restore"
    undo = {
        "target": "before",
        "expected_versions": {annotation["id"]: annotation["version"]},
        "idempotency_key": str(uuid.uuid4()),
        "video_segment_id": None,
    }
    blocked = await httpx_client.post(url, json=undo, headers=_headers(token))
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["reason"] == "task_lock_conflict"
    await db_session.refresh(
        await db_session.get(Annotation, uuid.UUID(annotation["id"]))
    )
    assert (await db_session.get(Annotation, uuid.UUID(annotation["id"]))).is_active
    assert await locks.release(task_id, other_id)
    await db_session.commit()
    undone = await httpx_client.post(url, json=undo, headers=_headers(token))
    assert undone.status_code == 200, undone.text
    assert await locks.acquire(task_id, other_id) is not None
    await db_session.commit()
    redo = {
        "target": "after",
        "expected_versions": undone.json()["result_versions"],
        "idempotency_key": str(uuid.uuid4()),
        "video_segment_id": None,
    }
    blocked = await httpx_client.post(url, json=redo, headers=_headers(token))
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["reason"] == "task_lock_conflict"
    await db_session.refresh(
        await db_session.get(Annotation, uuid.UUID(annotation["id"]))
    )
    assert not (await db_session.get(Annotation, uuid.UUID(annotation["id"]))).is_active
