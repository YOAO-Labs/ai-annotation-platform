from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy import inspect as sa_inspect

from app.db.models.async_job import AsyncJob
from app.db.models.notification import Notification
from app.services.async_job_notify import notify_job_terminal
from app.services.notification import NotificationService
from aap_notifications.terminal_append_postgres import (
    NotificationTerminalAppendPostgresData,
)


@pytest.mark.asyncio
async def test_legacy_notification_list_keeps_order_counts_and_dirty_identity(
    db_session, annotator
):
    user, _ = annotator
    service = NotificationService(db_session)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        Notification(
            id=uuid.uuid4(),
            user_id=user.id,
            type=f"read.baseline.{index}",
            target_type="task",
            target_id=uuid.uuid4(),
            payload={"stored": index},
            read_at=(start if index == 1 else None),
            created_at=start + timedelta(seconds=index),
        )
        for index in range(3)
    ]
    db_session.add_all(rows)
    await db_session.flush()

    cached = await db_session.get(Notification, rows[1].id)
    assert cached is rows[1]
    cached.read_at = None
    cached.payload = {"dirty": True}

    with db_session.no_autoflush:
        page, total, unread = await service.list_for_user(user.id, limit=2, offset=1)
        assert [row.id for row in page] == [rows[1].id, rows[0].id]
        assert page[0] is cached
        assert page[0].read_at is None
        assert page[0].payload == {"dirty": True}
        assert (total, unread) == (3, 2)

        unread_page, unread_total, unread_count = await service.list_for_user(
            user.id, unread_only=True, limit=2, offset=0
        )
        assert [row.id for row in unread_page] == [rows[2].id, rows[0].id]
        assert (unread_total, unread_count) == (3, 2)
        assert await service.unread_count(user.id) == 2
        stored = await db_session.scalar(
            select(Notification.read_at).where(Notification.id == rows[1].id)
        )
        assert stored == start


@pytest.mark.asyncio
async def test_inbox_query_preserves_native_jsonb_payload_shapes(db_session, annotator):
    user, _ = annotator
    payloads = [None, {}, [], ["project_id"], "scalar", 5, True]
    rows = [
        Notification(
            id=uuid.uuid4(),
            user_id=user.id,
            type=f"inbox.jsonb.{index}",
            target_type="task",
            target_id=uuid.uuid4(),
            payload=payload,
            created_at=datetime(2026, 2, 1, tzinfo=timezone.utc)
            + timedelta(seconds=index),
        )
        for index, payload in enumerate(payloads)
    ]
    db_session.add_all(rows)
    await db_session.flush()

    page, total, unread = await NotificationService(db_session).list_for_user(
        user.id,
        limit=len(rows),
    )

    assert total == unread == len(payloads)
    assert {row.id: row.payload for row in page} == {
        row.id: payload for row, payload in zip(rows, payloads, strict=True)
    }


@pytest.mark.asyncio
async def test_legacy_notification_get_keeps_cached_and_expired_identity_behavior(
    db_session, annotator, reviewer
):
    user, _ = annotator
    reviewer_user, _ = reviewer
    service = NotificationService(db_session)
    row = Notification(
        id=uuid.uuid4(),
        user_id=user.id,
        type="read.baseline.cache",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={"stored": True},
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(row)
    await db_session.flush()
    row_id = row.id

    cached = await db_session.get(Notification, row_id)
    assert cached is row
    cached.user_id = reviewer_user.id
    assert await db_session.get(Notification, row_id) is cached
    assert await service.notification_deliverable(row_id, reviewer_user.id) is True

    db_session.expire(cached)
    assert sa_inspect(cached).expired
    assert await service.notification_deliverable(row_id, user.id) is True
    assert db_session.identity_map.get(sa_inspect(cached).identity_key) is cached
    assert cached.user_id == user.id
    assert cached.payload == {"stored": True}


@pytest.mark.asyncio
async def test_legacy_terminal_dedupe_select_preserves_autoflush_modes(
    db_session, annotator, monkeypatch
):
    user, _ = annotator
    command = AsyncMock(
        return_value={
            "schema": "aap-notification-terminal-append/v1",
            "ok": False,
            "error": {"code": "invalid_request"},
        }
    )
    monkeypatch.setattr(NotificationTerminalAppendPostgresData, "command", command)

    def terminal_row(job: AsyncJob) -> Notification:
        return Notification(
            id=uuid.uuid4(),
            user_id=user.id,
            type="job.completed",
            target_type="async_job",
            target_id=job.id,
            payload={},
            created_at=datetime.now(timezone.utc),
        )

    auto_job = AsyncJob(
        id=uuid.uuid4(),
        kind="batch_predict",
        user_id=user.id,
        status="completed",
        payload={},
        result={},
    )
    db_session.add(auto_job)
    await db_session.flush()
    pending_auto = terminal_row(auto_job)
    db_session.add(pending_auto)

    await notify_job_terminal(db_session, job_id=auto_job.id)
    assert pending_auto not in db_session.new
    command.assert_not_awaited()

    no_autoflush_job = AsyncJob(
        id=uuid.uuid4(),
        kind="batch_predict",
        user_id=user.id,
        status="completed",
        payload={},
        result={},
    )
    db_session.add(no_autoflush_job)
    await db_session.flush()
    pending_no_autoflush = terminal_row(no_autoflush_job)
    db_session.add(pending_no_autoflush)

    with db_session.no_autoflush:
        await notify_job_terminal(db_session, job_id=no_autoflush_job.id)

    command.assert_awaited_once()
    assert pending_no_autoflush in db_session.new


@pytest.mark.asyncio
async def test_partial_expiration_get_matches_identity_map_shortcut(
    db_session, annotator
):
    user, _ = annotator
    row = Notification(
        id=uuid.uuid4(),
        user_id=user.id,
        type="read.baseline.partial-expiration",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={},
    )
    db_session.add(row)
    await db_session.flush()
    row_id = row.id
    cached = await db_session.get(Notification, row_id)
    assert cached is row

    statements = []

    def listener(_state):
        statements.append(True)

    event.listen(db_session.sync_session, "do_orm_execute", listener)
    try:
        db_session.expire(cached, ["read_at"])
        assert await db_session.get(Notification, row_id) is cached
        assert statements == []

        db_session.expire(cached, ["read_at"])
        assert (
            await NotificationService(db_session).notification_by_id(row_id) is cached
        )
        assert statements == []
        assert "read_at" in sa_inspect(cached).expired_attributes
    finally:
        event.remove(db_session.sync_session, "do_orm_execute", listener)


@pytest.mark.asyncio
async def test_fully_expired_get_and_relation_preserve_unrelated_autoflush(
    db_session, annotator
):
    user, _ = annotator
    legacy_row = Notification(
        id=uuid.uuid4(),
        user_id=user.id,
        type="read.baseline.full-expiration.legacy",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={},
    )
    relation_row = Notification(
        id=uuid.uuid4(),
        user_id=user.id,
        type="read.baseline.full-expiration.relation",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={},
    )
    db_session.add_all([legacy_row, relation_row])
    await db_session.flush()
    legacy_id, relation_id = legacy_row.id, relation_row.id
    legacy_cached = await db_session.get(Notification, legacy_id)
    relation_cached = await db_session.get(Notification, relation_id)

    db_session.expire(legacy_cached)
    pending_legacy = Notification(
        id=uuid.uuid4(),
        user_id=user.id,
        type="read.baseline.pending.legacy",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={},
    )
    db_session.add(pending_legacy)
    assert await db_session.get(Notification, legacy_id) is legacy_cached
    legacy_was_flushed = pending_legacy not in db_session.new

    db_session.expire(relation_cached)
    pending_relation = Notification(
        id=uuid.uuid4(),
        user_id=user.id,
        type="read.baseline.pending.relation",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={},
    )
    db_session.add(pending_relation)
    assert (
        await NotificationService(db_session).notification_by_id(relation_id)
        is relation_cached
    )
    relation_was_flushed = pending_relation not in db_session.new
    assert relation_was_flushed is legacy_was_flushed

    no_autoflush_legacy = Notification(
        id=uuid.uuid4(),
        user_id=user.id,
        type="read.baseline.no-autoflush.legacy",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={},
    )
    no_autoflush_relation = Notification(
        id=uuid.uuid4(),
        user_id=user.id,
        type="read.baseline.no-autoflush.relation",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={},
    )
    db_session.add_all([no_autoflush_legacy, no_autoflush_relation])
    await db_session.flush()
    no_autoflush_legacy_id = no_autoflush_legacy.id
    no_autoflush_relation_id = no_autoflush_relation.id
    no_autoflush_legacy_cached = await db_session.get(
        Notification, no_autoflush_legacy_id
    )
    no_autoflush_relation_cached = await db_session.get(
        Notification, no_autoflush_relation_id
    )
    db_session.expire(no_autoflush_legacy_cached)
    db_session.expire(no_autoflush_relation_cached)
    pending_legacy_no_autoflush = Notification(
        id=uuid.uuid4(),
        user_id=user.id,
        type="read.baseline.pending.no-autoflush.legacy",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={},
    )
    pending_relation_no_autoflush = Notification(
        id=uuid.uuid4(),
        user_id=user.id,
        type="read.baseline.pending.no-autoflush.relation",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={},
    )
    db_session.add_all([pending_legacy_no_autoflush, pending_relation_no_autoflush])
    with db_session.no_autoflush:
        assert (
            await db_session.get(Notification, no_autoflush_legacy_id)
            is no_autoflush_legacy_cached
        )
        assert (
            await NotificationService(db_session).notification_by_id(
                no_autoflush_relation_id
            )
            is no_autoflush_relation_cached
        )
    assert pending_legacy_no_autoflush in db_session.new
    assert pending_relation_no_autoflush in db_session.new


@pytest.mark.asyncio
async def test_missing_fully_expired_get_removes_identity_state(db_session, annotator):
    user, _ = annotator
    legacy_row = Notification(
        id=uuid.uuid4(),
        user_id=user.id,
        type="read.baseline.missing.legacy",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={},
    )
    relation_row = Notification(
        id=uuid.uuid4(),
        user_id=user.id,
        type="read.baseline.missing.relation",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={},
    )
    db_session.add_all([legacy_row, relation_row])
    await db_session.flush()
    legacy_id, relation_id = legacy_row.id, relation_row.id
    legacy_cached = await db_session.get(Notification, legacy_id)
    relation_cached = await db_session.get(Notification, relation_id)
    legacy_key = sa_inspect(legacy_cached).identity_key
    relation_key = sa_inspect(relation_cached).identity_key

    db_session.expire(legacy_cached)
    await db_session.execute(
        delete(Notification)
        .where(Notification.id == legacy_id)
        .execution_options(synchronize_session=False)
    )
    assert await db_session.get(Notification, legacy_id) is None
    assert db_session.sync_session.identity_map.get(legacy_key) is None

    db_session.expire(relation_cached)
    await db_session.execute(
        delete(Notification)
        .where(Notification.id == relation_id)
        .execution_options(synchronize_session=False)
    )
    assert await NotificationService(db_session).notification_by_id(relation_id) is None
    assert db_session.sync_session.identity_map.get(relation_key) is None
