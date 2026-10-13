"""Native task-comment mention contracts and notification fan-out."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.notification import Notification
from app.db.models.notification_preference import NotificationPreference
from app.db.models.project_member import ProjectMember
from app.schemas.annotation_feedback import AnnotationFeedbackCreate
from tests.factory import (
    create_membership,
    create_batch,
    create_project,
    create_task,
    create_user,
)


def _headers(user, token: str | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token or create_access_token(subject=str(user.id), role=user.role)}"
    }


def _mention(user_id: UUID, name: str = "Mentioned") -> dict[str, object]:
    return {
        "userId": str(user_id),
        "displayName": name,
        "offset": 0,
        "length": len(name) + 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("file_type", ["image", "video", "point_cloud"])
async def test_native_task_comment_mentions_round_trip_and_notify_once(
    httpx_client, db_session, super_admin, monkeypatch, file_type
):
    actor, actor_token = super_admin
    recipient = await create_user(
        db_session, "employee", f"mention-{uuid4().hex}@test.local", "Mentioned"
    )
    project = await create_project(db_session, owner_id=actor.id)
    batch = await create_batch(db_session, project_id=project.id, status="active")
    task = await create_task(db_session, project_id=project.id, status="in_progress")
    task.file_type = file_type
    batch.annotator_id = recipient.id
    task.batch_id = batch.id
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=recipient.id,
        role="annotator",
        assigned_by=actor.id,
    )
    await db_session.flush()

    published: list[dict] = []

    async def fake_publish(*, user_id, message):
        published.append({"user_id": user_id, **message})

    monkeypatch.setattr("app.services.notification._publish", fake_publish)
    mention = _mention(recipient.id)
    payload = {
        "kind": "comment",
        "anchor_type": "task",
        "project_id": str(project.id),
        "task_id": str(task.id),
        "body": "@Mentioned please check",
        "mentions": [mention, mention, _mention(actor.id, actor.name)],
    }
    response = await httpx_client.post(
        "/api/v1/feedbacks", json=payload, headers=_headers(actor, actor_token)
    )
    assert response.status_code == 200, response.text
    assert response.json()["mentions"] == payload["mentions"]

    feed = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/discussion/page",
        params={"scope": "task"},
        headers=_headers(actor, actor_token),
    )
    assert feed.status_code == 200, feed.text
    assert feed.json()["items"][0]["source"] == "feedback"
    assert feed.json()["items"][0]["data"]["mentions"] == payload["mentions"]

    rows = list(
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.type == "feedback.comment_mentioned",
                    Notification.target_id == UUID(response.json()["id"]),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].user_id == recipient.id
    assert rows[0].target_type == "feedback"
    assert rows[0].payload["source"] == "feedback"
    assert [event["user_id"] for event in published] == [recipient.id]
    preferences = await httpx_client.get(
        "/api/v1/notification-preferences", headers=_headers(actor, actor_token)
    )
    assert preferences.status_code == 200, preferences.text
    assert "feedback.comment_mentioned" in {
        item["type"] for item in preferences.json()["items"]
    }

    unchanged = await httpx_client.patch(
        f"/api/v1/feedbacks/{response.json()['id']}",
        json={"body": payload["body"]},
        headers=_headers(actor, actor_token),
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["mentions"] == payload["mentions"]

    patched = await httpx_client.patch(
        f"/api/v1/feedbacks/{response.json()['id']}",
        json={"body": "edited task comment"},
        headers=_headers(actor, actor_token),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["mentions"] == []


@pytest.mark.asyncio
async def test_mentions_reject_foreign_destinations_and_replies(
    httpx_client, db_session, super_admin
):
    actor, token = super_admin
    recipient = await create_user(
        db_session, "employee", f"foreign-{uuid4().hex}@test.local", "Foreign"
    )
    project = await create_project(db_session, owner_id=actor.id)
    task = await create_task(db_session, project_id=project.id)
    headers = _headers(actor, token)
    mention = _mention(recipient.id, "Foreign")

    foreign = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "comment",
            "anchor_type": "task",
            "project_id": str(project.id),
            "task_id": str(task.id),
            "body": "@Foreign",
            "mentions": [mention],
        },
        headers=headers,
    )
    assert foreign.status_code == 422
    assert foreign.json()["detail"]["error"] == "mentions_invalid"

    unsupported = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "comment",
            "anchor_type": "project",
            "project_id": str(project.id),
            "body": "@Foreign",
            "mentions": [mention],
        },
        headers=headers,
    )
    assert unsupported.status_code == 422

    root = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "comment",
            "anchor_type": "task",
            "project_id": str(project.id),
            "task_id": str(task.id),
            "body": "root",
        },
        headers=headers,
    )
    assert root.status_code == 200, root.text
    reply = await httpx_client.post(
        f"/api/v1/feedbacks/{root.json()['id']}/replies",
        json={"body": "reply", "mentions": [mention]},
        headers=headers,
    )
    assert reply.status_code == 422
    plain_reply = await httpx_client.post(
        f"/api/v1/feedbacks/{root.json()['id']}/replies",
        json={"body": "plain reply"},
        headers=headers,
    )
    assert plain_reply.status_code == 200, plain_reply.text
    reply_patch = await httpx_client.patch(
        f"/api/v1/feedbacks/{plain_reply.json()['id']}",
        json={"mentions": [mention]},
        headers=headers,
    )
    assert reply_patch.status_code == 422


@pytest.mark.asyncio
async def test_task_comment_mentions_skip_hidden_inactive_and_muted_recipients(
    httpx_client, db_session, super_admin
):
    actor, token = super_admin
    assigned = await create_user(
        db_session, "employee", f"assigned-{uuid4().hex}@test.local", "Assigned"
    )
    inactive = await create_user(
        db_session, "employee", f"inactive-{uuid4().hex}@test.local", "Inactive"
    )
    unassigned = await create_user(
        db_session, "employee", f"unassigned-{uuid4().hex}@test.local", "Unassigned"
    )
    inactive.is_active = False
    project = await create_project(db_session, owner_id=actor.id)
    batch = await create_batch(db_session, project_id=project.id, status="active")
    task = await create_task(db_session, project_id=project.id, status="in_progress")
    batch.annotator_id = assigned.id
    task.batch_id = batch.id
    db_session.add_all(
        [
            ProjectMember(
                project_id=project.id,
                user_id=user.id,
                role="annotator",
                assigned_by=actor.id,
            )
            for user in (assigned, inactive, unassigned)
        ]
    )
    await db_session.flush()
    headers = _headers(actor, token)
    payload = {
        "kind": "comment",
        "anchor_type": "task",
        "project_id": str(project.id),
        "task_id": str(task.id),
        "body": "mentions",
        "mentions": [
            _mention(assigned.id, assigned.name),
            _mention(inactive.id, inactive.name),
            _mention(unassigned.id, unassigned.name),
        ],
    }
    first = await httpx_client.post("/api/v1/feedbacks", json=payload, headers=headers)
    assert first.status_code == 200, first.text
    first_rows = list(
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.target_id == UUID(first.json()["id"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert [row.user_id for row in first_rows] == [assigned.id]

    db_session.add(
        NotificationPreference(
            user_id=assigned.id,
            type="feedback.comment_mentioned",
            channels={"in_app": False, "email": False},
        )
    )
    await db_session.flush()
    second = await httpx_client.post(
        "/api/v1/feedbacks",
        json={**payload, "body": "muted"},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    second_rows = list(
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.target_id == UUID(second.json()["id"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert second_rows == []


@pytest.mark.asyncio
async def test_new_image_issue_rejects_unavailable_annotation_but_old_reply_survives(
    httpx_client, db_session, super_admin
):
    actor, token = super_admin
    project = await create_project(db_session, owner_id=actor.id)
    task = await create_task(db_session, project_id=project.id)
    annotation = Annotation(
        id=uuid4(),
        task_id=task.id,
        project_id=project.id,
        user_id=actor.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        attributes={},
        is_active=True,
    )
    db_session.add(annotation)
    await db_session.flush()
    headers = _headers(actor, token)
    position = {"x": 0.2, "y": 0.2, "frame": 0}

    root = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "issue",
            "anchor_type": "pixel",
            "project_id": str(project.id),
            "task_id": str(task.id),
            "annotation_id": str(annotation.id),
            "anchor_position": position,
            "body": "root before object removal",
        },
        headers=headers,
    )
    assert root.status_code == 200, root.text

    annotation.is_active = False
    await db_session.flush()
    new_root = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "issue",
            "anchor_type": "pixel",
            "project_id": str(project.id),
            "task_id": str(task.id),
            "annotation_id": str(annotation.id),
            "anchor_position": position,
            "body": "new issue must reject removed object",
        },
        headers=headers,
    )
    assert new_root.status_code == 422
    assert new_root.json()["detail"]["reason"] == "feedback_annotation_unavailable"

    old_reply = await httpx_client.post(
        f"/api/v1/feedbacks/{root.json()['id']}/replies",
        json={"body": "historical reply remains allowed"},
        headers=headers,
    )
    assert old_reply.status_code == 200, old_reply.text


def test_native_root_task_comment_schema_accepts_mentions():
    """Only native root task comments accept structured mentions."""

    # The route test above exercises the real mutation. This schema assertion keeps
    # the native root destination rule visible without requiring another database.
    payload = AnnotationFeedbackCreate(
        kind="comment",
        anchor_type="task",
        project_id=uuid4(),
        task_id=uuid4(),
        body="@Mentioned",
        mentions=[_mention(uuid4())],
    )
    assert payload.mentions[0].display_name == "Mentioned"
