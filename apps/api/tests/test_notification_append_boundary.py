from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from aap_notifications.preferences import NotificationPreferenceManager
from aap_notifications.preferences_postgres import NotificationPreferencesPostgresData
from app.db.models.notification import Notification
from app.services.notification import NotificationService


@pytest.mark.asyncio
async def test_notify_returns_server_row_in_identity_map_without_committing(
    db_session, annotator
):
    user, _ = annotator
    target_id = uuid.uuid4()
    service = NotificationService(db_session)

    row = await service.notify(
        user_id=user.id,
        type="future.notification",
        target_type="task",
        target_id=target_id,
        payload={"task_id": str(target_id), "empty": []},
    )

    assert row is not None
    assert row.id is not None
    assert row.created_at.tzinfo is not None
    assert row.read_at is None
    assert row.payload == {"task_id": str(target_id), "empty": []}
    assert await db_session.get(Notification, row.id) is row
    assert db_session.in_transaction()

    stored = await db_session.scalar(
        select(Notification).where(Notification.id == row.id)
    )
    assert stored is row
    assert stored.created_at == row.created_at


@pytest.mark.asyncio
async def test_notify_preserves_pending_recipient_delete_flush_order(
    db_session, annotator
):
    user, _ = annotator
    await db_session.delete(user)

    with db_session.no_autoflush:
        row = await NotificationService(db_session).notify(
            user_id=user.id,
            type="future.notification",
            target_type="task",
            target_id=uuid.uuid4(),
        )

    # The legacy same-flush ORM path inserts before deleting the recipient;
    # the user's ON DELETE CASCADE then removes the notification row.
    assert row is not None
    assert (
        await db_session.scalar(
            select(Notification.id).where(Notification.id == row.id)
        )
        is None
    )


@pytest.mark.asyncio
async def test_notify_uses_the_callers_savepoint_for_rollback(db_session, annotator):
    user, _ = annotator
    service = NotificationService(db_session)
    savepoint = await db_session.begin_nested()

    row = await service.notify(
        user_id=user.id,
        type="task.approved",
        target_type="task",
        target_id=uuid.uuid4(),
    )
    assert row is not None
    row_id = row.id

    await savepoint.rollback()
    if row in db_session:
        db_session.expunge(row)
    assert await db_session.get(Notification, row_id) is None


@pytest.mark.asyncio
async def test_notify_many_keeps_recipient_order_dedup_and_muted_behavior(
    db_session, annotator, reviewer, project_admin, monkeypatch
):
    first, _ = annotator
    muted, _ = reviewer
    last, _ = project_admin
    await NotificationPreferenceManager(
        NotificationPreferencesPostgresData(db_session)
    ).patch(
        muted.id,
        "batch.review_reopened",
        {"in_app": False},
    )
    service = NotificationService(db_session)
    published = []

    async def record_publish(rows):
        published.append(rows)

    monkeypatch.setattr(service, "publish_committed", record_publish)
    rows = await service.notify_many(
        user_ids=[first.id, muted.id, first.id, last.id],
        type="batch.review_reopened",
        target_type="batch",
        target_id=uuid.uuid4(),
        payload={"batch": "b-1"},
        defer_publish=False,
    )

    assert [row.user_id for row in rows] == [first.id, last.id]
    assert all(row.payload == {"batch": "b-1"} for row in rows)
    assert published == [rows]
