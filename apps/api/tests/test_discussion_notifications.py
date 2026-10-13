"""Discussion notification fan-out and transaction-boundary regressions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.notification import Notification
from app.db.models.notification_preference import NotificationPreference
from app.services.notification import NotificationService
from tests.factory import (
    create_batch,
    create_membership,
    create_project,
    create_task,
    create_user,
)


def _headers(user, token: str | None = None) -> dict[str, str]:
    token = token or create_access_token(subject=str(user.id), role=user.role)
    return {"Authorization": f"Bearer {token}"}


async def _add_member(db, project_id, user, role: str | None = None, assigned_by=None):
    await create_membership(
        db,
        project_id=project_id,
        user_id=user.id,
        role=role or user.role,
        assigned_by=assigned_by,
    )


async def _seed_task_scope(db, owner, assignee):
    project = await create_project(db, owner_id=owner.id)
    batch = await create_batch(db, project_id=project.id, status="active")
    task = await create_task(db, project_id=project.id)
    batch.annotator_id = assignee.id
    task.batch_id = batch.id
    await db.flush()
    return project, batch, task


async def _create_issue(client, project, task, user, *, token: str | None = None):
    response = await client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "issue",
            "anchor_type": "task",
            "project_id": str(project.id),
            "task_id": str(task.id),
            "body": "root issue",
        },
        headers=_headers(user, token),
    )
    assert response.status_code == 200, response.text
    return UUID(response.json()["id"])


def _feedback_reply(
    *, author_id, project_id, task_id, parent_id, active=True, created_at
):
    return AnnotationFeedback(
        id=uuid4(),
        kind="comment",
        anchor_type="task",
        project_id=project_id,
        task_id=task_id,
        annotation_id=None,
        anchor_position=None,
        severity=None,
        title=None,
        body="reply",
        author_id=author_id,
        attachments=[],
        thread_parent_id=parent_id,
        status="open",
        is_active=active,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_feedback_reply_events_cover_generic_and_dedicated_routes(
    httpx_client, db_session, super_admin, annotator, reviewer, monkeypatch
):
    owner, _ = super_admin
    author, author_token = annotator
    qa, qa_token = reviewer
    project, _batch, task = await _seed_task_scope(db_session, owner, author)
    await _add_member(db_session, project.id, author, "annotator", owner.id)
    await _add_member(db_session, project.id, qa, "reviewer", owner.id)

    published: list[tuple] = []

    async def fake_publish(*, user_id, message):
        published.append((user_id, message))

    monkeypatch.setattr("app.services.notification._publish", fake_publish)

    root_id = await _create_issue(
        httpx_client, project, task, author, token=author_token
    )
    invalid = await httpx_client.post(
        f"/api/v1/feedbacks/{root_id}/replies",
        json={"body": "  \n"},
        headers=_headers(qa, qa_token),
    )
    assert invalid.status_code == 422
    assert published == []

    direct_invalid = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "comment",
            "anchor_type": "task",
            "project_id": str(project.id),
            "task_id": str(task.id),
            "body": " \t",
            "thread_parent_id": str(root_id),
        },
        headers=_headers(qa, qa_token),
    )
    assert direct_invalid.status_code == 422
    assert published == []

    direct = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "comment",
            "anchor_type": "task",
            "project_id": str(project.id),
            "task_id": str(task.id),
            "body": "generic reply",
            "thread_parent_id": str(root_id),
        },
        headers=_headers(qa, qa_token),
    )
    assert direct.status_code == 200, direct.text
    direct_id = UUID(direct.json()["id"])

    dedicated = await httpx_client.post(
        f"/api/v1/feedbacks/{root_id}/replies",
        json={"body": "dedicated reply"},
        headers=_headers(qa, qa_token),
    )
    assert dedicated.status_code == 200, dedicated.text
    dedicated_id = UUID(dedicated.json()["id"])

    # The root author replying to their own Issue is excluded from the event.
    own_reply = await httpx_client.post(
        f"/api/v1/feedbacks/{root_id}/replies",
        json={"body": "author reply"},
        headers=_headers(author, author_token),
    )
    assert own_reply.status_code == 200, own_reply.text

    rows = list(
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.type == "feedback.reply_created",
                    Notification.target_id == root_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert {row.user_id for row in rows} == {author.id}
    assert {row.payload["reply_id"] for row in rows} == {
        str(direct_id),
        str(dedicated_id),
    }
    assert all(row.target_type == "feedback" for row in rows)
    assert [message[0] for message in published] == [author.id, author.id]


@pytest.mark.asyncio
async def test_feedback_status_event_reaches_all_active_descendant_authors(
    httpx_client, db_session, super_admin, annotator, reviewer, monkeypatch
):
    owner, owner_token = super_admin
    author, _ = annotator
    qa, _ = reviewer
    old_only = await create_user(
        db_session,
        "employee",
        f"old-only-{uuid4().hex}@test.local",
        "Old-only",
    )
    grandchild_only = await create_user(
        db_session,
        "employee",
        f"grandchild-only-{uuid4().hex}@test.local",
        "Grandchild-only",
    )
    inactive = await create_user(
        db_session, "employee", f"inactive-{uuid4().hex}@test.local", "Inactive"
    )
    away = await create_user(
        db_session, "employee", f"away-{uuid4().hex}@test.local", "Away"
    )
    inactive.is_active = False
    project, batch, task = await _seed_task_scope(db_session, owner, author)
    for user, role in (
        (author, "annotator"),
        (qa, "reviewer"),
        (old_only, "reviewer"),
        (grandchild_only, "reviewer"),
        (inactive, "annotator"),
        (away, "annotator"),
    ):
        await _add_member(db_session, project.id, user, role, owner.id)
    await db_session.flush()

    published: list[tuple] = []

    async def fake_publish(*, user_id, message):
        published.append((user_id, message))

    monkeypatch.setattr("app.services.notification._publish", fake_publish)
    root_id = await _create_issue(httpx_client, project, task, author)
    root = await db_session.get(AnnotationFeedback, root_id)
    assert root is not None

    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    deleted_intermediate = _feedback_reply(
        author_id=qa.id,
        project_id=project.id,
        task_id=task.id,
        parent_id=root.id,
        active=False,
        created_at=now,
    )
    surviving_grandchild = _feedback_reply(
        author_id=grandchild_only.id,
        project_id=project.id,
        task_id=task.id,
        parent_id=deleted_intermediate.id,
        created_at=now + timedelta(seconds=1),
    )
    old_reply = _feedback_reply(
        author_id=old_only.id,
        project_id=project.id,
        task_id=task.id,
        parent_id=root.id,
        created_at=now - timedelta(seconds=1),
    )
    many_replies = [
        _feedback_reply(
            author_id=qa.id,
            project_id=project.id,
            task_id=task.id,
            parent_id=root.id,
            created_at=now + timedelta(seconds=2 + index),
        )
        for index in range(60)
    ]
    author_reply = _feedback_reply(
        author_id=author.id,
        project_id=project.id,
        task_id=task.id,
        parent_id=root.id,
        created_at=now + timedelta(seconds=100),
    )
    inactive_reply = _feedback_reply(
        author_id=inactive.id,
        project_id=project.id,
        task_id=task.id,
        parent_id=root.id,
        created_at=now + timedelta(seconds=101),
    )
    away_reply = _feedback_reply(
        author_id=away.id,
        project_id=project.id,
        task_id=task.id,
        parent_id=root.id,
        created_at=now + timedelta(seconds=102),
    )
    db_session.add_all(
        [
            deleted_intermediate,
            surviving_grandchild,
            old_reply,
            *many_replies,
            author_reply,
            inactive_reply,
            away_reply,
        ]
    )
    await db_session.flush()

    # A descendant status change is not a root Issue status change.
    descendant_patch = await httpx_client.patch(
        f"/api/v1/feedbacks/{many_replies[0].id}",
        json={"status": "resolved"},
        headers=_headers(owner, owner_token),
    )
    assert descendant_patch.status_code == 200, descendant_patch.text
    assert published == []

    # A no-op root status request is not a status transition and must not emit.
    noop_patch = await httpx_client.patch(
        f"/api/v1/feedbacks/{root_id}",
        json={"status": "open"},
        headers=_headers(owner, owner_token),
    )
    assert noop_patch.status_code == 200, noop_patch.text
    assert published == []

    root_patch = await httpx_client.patch(
        f"/api/v1/feedbacks/{root_id}",
        json={"status": "resolved"},
        headers=_headers(owner, owner_token),
    )
    assert root_patch.status_code == 200, root_patch.text

    rows = list(
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.type == "feedback.status_changed",
                    Notification.target_id == root_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 4
    assert {row.user_id for row in rows} == {
        author.id,
        qa.id,
        old_only.id,
        grandchild_only.id,
    }
    assert {row.payload["from_status"] for row in rows} == {"open"}
    assert {row.payload["to_status"] for row in rows} == {"resolved"}
    assert all(row.payload["source"] == "feedback" for row in rows)
    assert all(row.target_type == "feedback" for row in rows)
    assert {user_id for user_id, _message in published} == {
        author.id,
        qa.id,
        old_only.id,
        grandchild_only.id,
    }
    assert batch.annotator_id == author.id


@pytest.mark.asyncio
async def test_annotation_mentions_notify_original_comment_only_and_filter_access(
    httpx_client, db_session, super_admin, reviewer, monkeypatch
):
    owner, owner_token = super_admin
    qa, _ = reviewer
    inactive = await create_user(
        db_session,
        "employee",
        f"inactive-mention-{uuid4().hex}@test.local",
        "Inactive",
    )
    away = await create_user(
        db_session, "employee", f"away-mention-{uuid4().hex}@test.local", "Away"
    )
    project, batch, task = await _seed_task_scope(db_session, owner, qa)
    for user, role in (
        (qa, "reviewer"),
        (inactive, "annotator"),
        (away, "annotator"),
    ):
        await _add_member(db_session, project.id, user, role, owner.id)
    inactive.is_active = False
    # The reviewer owns the active batch, so the annotator recipient is assigned away.
    batch.annotator_id = qa.id
    annotation = Annotation(
        id=uuid4(),
        task_id=task.id,
        project_id=project.id,
        user_id=owner.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        attributes={},
        is_active=True,
    )
    db_session.add(annotation)
    await db_session.flush()

    published: list[tuple] = []

    async def fake_publish(*, user_id, message):
        published.append((user_id, message))

    monkeypatch.setattr("app.services.notification._publish", fake_publish)
    mentions = [
        {
            "userId": str(qa.id),
            "displayName": qa.name,
            "offset": 0,
            "length": 2,
        },
        {
            "userId": str(qa.id),
            "displayName": qa.name,
            "offset": 3,
            "length": 2,
        },
        {
            "userId": str(owner.id),
            "displayName": owner.name,
            "offset": 6,
            "length": 2,
        },
        {
            "userId": str(inactive.id),
            "displayName": inactive.name,
            "offset": 9,
            "length": 2,
        },
        {
            "userId": str(away.id),
            "displayName": away.name,
            "offset": 12,
            "length": 2,
        },
    ]
    response = await httpx_client.post(
        f"/api/v1/annotations/{annotation.id}/comments",
        json={"body": "Please review this area", "mentions": mentions},
        headers=_headers(owner, owner_token),
    )
    assert response.status_code == 201, response.text
    comment_id = UUID(response.json()["id"])

    notifications = list(
        (
            await db_session.execute(
                select(Notification).where(Notification.target_id == comment_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification.user_id == qa.id
    assert notification.type == "annotation.comment_mentioned"
    assert notification.target_type == "annotation_comment"
    assert notification.payload == {
        "project_id": str(project.id),
        "task_id": str(task.id),
        "source": "annotation_comment",
        "actor_name": owner.name,
        "annotation_id": str(annotation.id),
    }
    assert [user_id for user_id, _message in published] == [qa.id]

    # The source comment and its mirror coexist, but only the source target got an event.
    mirrors = list(
        (
            await db_session.execute(
                select(AnnotationFeedback).where(
                    AnnotationFeedback.kind == "comment",
                    AnnotationFeedback.anchor_type == "annotation",
                    AnnotationFeedback.annotation_id == annotation.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(mirrors) == 1
    assert all(row.target_id != mirrors[0].id for row in notifications)


@pytest.mark.asyncio
async def test_feedback_status_route_honors_mute_and_noop_transition(
    httpx_client, db_session, super_admin, annotator, reviewer, monkeypatch
):
    owner, owner_token = super_admin
    author, _ = annotator
    qa, qa_token = reviewer
    project, _batch, task = await _seed_task_scope(db_session, owner, author)
    await _add_member(db_session, project.id, author, "annotator", owner.id)
    await _add_member(db_session, project.id, qa, "reviewer", owner.id)
    db_session.add(
        NotificationPreference(
            user_id=author.id,
            type="feedback.status_changed",
            channels={"in_app": False, "email": False},
        )
    )
    await db_session.flush()

    published: list[tuple] = []

    async def fake_publish(*, user_id, message):
        published.append((user_id, message))

    monkeypatch.setattr("app.services.notification._publish", fake_publish)
    root_id = await _create_issue(httpx_client, project, task, author)

    noop = await httpx_client.patch(
        f"/api/v1/feedbacks/{root_id}",
        json={"status": "open"},
        headers=_headers(qa, qa_token),
    )
    assert noop.status_code == 200, noop.text
    assert published == []

    muted = await httpx_client.patch(
        f"/api/v1/feedbacks/{root_id}",
        json={"status": "resolved"},
        headers=_headers(qa, qa_token),
    )
    assert muted.status_code == 200, muted.text
    assert published == []
    rows = list(
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.type == "feedback.status_changed",
                    Notification.target_id == root_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_deferred_notification_publish_is_default(
    db_session, annotator, monkeypatch
):
    user, _ = annotator
    messages: list[dict] = []

    async def fake_publish(*, user_id, message):
        messages.append(message)

    monkeypatch.setattr("app.services.notification._publish", fake_publish)
    service = NotificationService(db_session)
    deferred = await service.notify(
        user_id=user.id,
        type="feedback.reply_created",
        target_type="feedback",
        target_id=uuid4(),
        payload={"source": "feedback"},
        defer_publish=True,
    )
    assert deferred is not None
    assert messages == []

    await service.publish_committed([deferred])
    assert len(messages) == 1
    deferred_many = await service.notify_many(
        user_ids=[user.id, user.id],
        type="feedback.status_changed",
        target_type="feedback",
        target_id=uuid4(),
        defer_publish=True,
    )
    assert len(deferred_many) == 1
    assert len(messages) == 1
    await service.publish_committed(deferred_many)
    assert len(messages) == 2

    # Publication is deferred by default: the row is written but nothing is
    # pushed until the caller publishes after its commit.
    default_deferred = await service.notify(
        user_id=user.id,
        type="legacy.type",
        target_type="task",
        target_id=uuid4(),
    )
    assert default_deferred is not None
    assert len(messages) == 2
    await service.publish_committed([default_deferred])
    assert len(messages) == 3

    # Legacy explicit opt-in still publishes immediately.
    immediate = await service.notify(
        user_id=user.id,
        type="legacy.type",
        target_type="task",
        target_id=uuid4(),
        defer_publish=False,
    )
    assert immediate is not None
    assert len(messages) == 4

    db_session.add(
        NotificationPreference(
            user_id=user.id,
            type="feedback.reply_created",
            channels={"in_app": False, "email": False},
        )
    )
    await db_session.flush()
    muted = await service.notify(
        user_id=user.id,
        type="feedback.reply_created",
        target_type="feedback",
        target_id=uuid4(),
        defer_publish=True,
    )
    assert muted is None
    assert len(messages) == 4


@pytest.mark.asyncio
async def test_feedback_reply_commit_survives_redis_publish_failure(
    httpx_client, db_session, super_admin, annotator, reviewer, monkeypatch
):
    owner, _ = super_admin
    author, author_token = annotator
    qa, qa_token = reviewer
    project, _batch, task = await _seed_task_scope(db_session, owner, author)
    await _add_member(db_session, project.id, author, "annotator", owner.id)
    await _add_member(db_session, project.id, qa, "reviewer", owner.id)

    async def failing_publish(*, user_id, message):
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr("app.services.notification._publish", failing_publish)
    root_id = await _create_issue(
        httpx_client, project, task, author, token=author_token
    )
    response = await httpx_client.post(
        f"/api/v1/feedbacks/{root_id}/replies",
        json={"body": "reply remains durable"},
        headers=_headers(qa, qa_token),
    )
    assert response.status_code == 200, response.text
    reply_id = UUID(response.json()["id"])

    persisted_reply = await db_session.get(AnnotationFeedback, reply_id)
    assert persisted_reply is not None
    rows = list(
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.type == "feedback.reply_created",
                    Notification.target_id == root_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].user_id == author.id
