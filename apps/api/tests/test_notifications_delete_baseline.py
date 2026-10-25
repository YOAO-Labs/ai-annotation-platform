"""Characterize the existing ORM DELETE behavior before Notifications extraction."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, update

from app.db.models.notification import Notification
from app.services.notification import NotificationService
from sqlalchemy import inspect as sa_inspect


def _row(user_id: uuid.UUID, *, read_at: datetime | None = None) -> Notification:
    return Notification(
        id=uuid.uuid4(),
        user_id=user_id,
        type="delete-baseline.test",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={},
        read_at=read_at,
    )


def _state(row: Notification, session) -> dict[str, object]:
    state = sa_inspect(row)
    return {
        "persistent": state.persistent,
        "deleted": state.deleted,
        "detached": state.detached,
        "attached": row in session,
        "new": row in session.new,
        "dirty": row in session.dirty,
        "expired": sorted(state.expired_attributes),
    }


async def _exists(session, notification_id: uuid.UUID) -> bool:
    return (
        await session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.id == notification_id)
        )
    ) == 1


@pytest.mark.asyncio
async def test_legacy_delete_for_user_loaded_and_rollback_baseline(
    db_session, annotator, reviewer
):
    user, _ = annotator
    other_user, _ = reviewer
    target = _row(user.id)
    foreign = _row(other_user.id)
    db_session.add_all([target, foreign])
    await db_session.flush()

    target_id = target.id
    nested = await db_session.begin_nested()
    deleted = await NotificationService(db_session).delete_for_user(user.id, target_id)
    wrong_scope = await NotificationService(db_session).delete_for_user(
        user.id, foreign.id
    )
    after_delete = {
        "deleted": deleted,
        "wrong_scope": wrong_scope,
        "target": _state(target, db_session),
        "foreign": _state(foreign, db_session),
        "target_in_database": await _exists(db_session, target_id),
    }
    await nested.rollback()
    after_rollback = {
        "target": _state(target, db_session),
        "target_in_database": await _exists(db_session, target_id),
    }
    print("legacy_delete_for_user_loaded", json.dumps(after_delete, sort_keys=True))
    print("legacy_delete_for_user_rollback", json.dumps(after_rollback, sort_keys=True))
    assert deleted is True
    assert wrong_scope is False
    assert after_delete["target_in_database"] is False
    assert after_rollback["target_in_database"] is True


@pytest.mark.asyncio
async def test_legacy_delete_for_user_expired_identity_baseline(db_session, annotator):
    user, _ = annotator
    target = _row(user.id)
    db_session.add(target)
    await db_session.flush()
    target_id = target.id

    nested = await db_session.begin_nested()
    db_session.expire(target)
    before = _state(target, db_session)
    deleted = await NotificationService(db_session).delete_for_user(user.id, target_id)
    after_delete = {
        "deleted": deleted,
        "state": _state(target, db_session),
        "in_database": await _exists(db_session, target_id),
    }
    await nested.rollback()
    after_rollback = {
        "state": _state(target, db_session),
        "in_database": await _exists(db_session, target_id),
    }
    print("legacy_delete_for_user_expired_before", json.dumps(before, sort_keys=True))
    print(
        "legacy_delete_for_user_expired_after", json.dumps(after_delete, sort_keys=True)
    )
    print(
        "legacy_delete_for_user_expired_rollback",
        json.dumps(after_rollback, sort_keys=True),
    )
    assert deleted is True
    assert after_delete["in_database"] is False
    assert after_rollback["in_database"] is True


@pytest.mark.asyncio
async def test_legacy_clear_read_autoflush_scope_and_rollback_baseline(
    db_session, annotator, reviewer
):
    user, _ = annotator
    other_user, _ = reviewer
    read_at = datetime.now(timezone.utc)
    read = _row(user.id, read_at=read_at)
    unread = _row(user.id)
    other_read = _row(other_user.id, read_at=read_at)
    db_session.add_all([read, unread, other_read])
    await db_session.flush()

    nested = await db_session.begin_nested()
    pending_read = _row(user.id, read_at=read_at)
    db_session.add(pending_read)
    deleted = await NotificationService(db_session).clear_read(user.id)
    after_delete = {
        "deleted": deleted,
        "read": _state(read, db_session),
        "pending_read": _state(pending_read, db_session),
        "unread": _state(unread, db_session),
        "other_read": _state(other_read, db_session),
        "own_read_rows": await db_session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user.id, Notification.read_at.is_not(None))
        ),
    }
    await nested.rollback()
    after_rollback = {
        "read": _state(read, db_session),
        "pending_read": _state(pending_read, db_session),
        "read_in_database": await _exists(db_session, read.id),
    }
    print("legacy_clear_read_autoflush", json.dumps(after_delete, sort_keys=True))
    print(
        "legacy_clear_read_autoflush_rollback",
        json.dumps(after_rollback, sort_keys=True),
    )
    assert deleted == 2
    assert after_delete["own_read_rows"] == 0
    assert after_rollback["read_in_database"] is True


@pytest.mark.asyncio
async def test_legacy_clear_read_no_autoflush_pending_baseline(db_session, annotator):
    user, _ = annotator
    persisted_read = _row(user.id, read_at=datetime.now(timezone.utc))
    db_session.add(persisted_read)
    await db_session.flush()

    nested = await db_session.begin_nested()
    pending_read = _row(user.id, read_at=datetime.now(timezone.utc))
    db_session.add(pending_read)
    with db_session.no_autoflush:
        deleted = await NotificationService(db_session).clear_read(user.id)
        state = {
            "deleted": deleted,
            "pending": _state(pending_read, db_session),
            "persisted": _state(persisted_read, db_session),
            "read_rows_in_database": await db_session.scalar(
                select(func.count())
                .select_from(Notification)
                .where(
                    Notification.user_id == user.id,
                    Notification.read_at.is_not(None),
                )
            ),
        }
    await nested.rollback()
    state["pending_after_rollback"] = _state(pending_read, db_session)
    print("legacy_clear_read_no_autoflush_pending", json.dumps(state, sort_keys=True))
    assert deleted == 1
    assert state["pending"]["new"] is True
    assert state["read_rows_in_database"] == 0


@pytest.mark.asyncio
async def test_legacy_clear_read_no_autoflush_dirty_predicate_baseline(
    db_session, annotator
):
    user, _ = annotator
    read = _row(user.id, read_at=datetime.now(timezone.utc))
    db_session.add(read)
    await db_session.flush()
    read_id = read.id

    nested = await db_session.begin_nested()
    read.read_at = None
    with db_session.no_autoflush:
        deleted = await NotificationService(db_session).clear_read(user.id)
        after_delete = {
            "deleted": deleted,
            "row": _state(read, db_session),
            "in_database": await _exists(db_session, read_id),
        }
    await nested.rollback()
    after_rollback = {
        "row": _state(read, db_session),
        "in_database": await _exists(db_session, read_id),
    }
    print(
        "legacy_clear_read_no_autoflush_dirty_read_at",
        json.dumps(after_delete, sort_keys=True),
    )
    print(
        "legacy_clear_read_no_autoflush_dirty_read_at_rollback",
        json.dumps(after_rollback, sort_keys=True),
    )
    assert deleted == 1
    assert after_delete["in_database"] is False
    assert after_rollback["in_database"] is True


@pytest.mark.asyncio
async def test_legacy_clear_read_no_autoflush_dirty_read_false_positive_baseline(
    db_session, annotator
):
    user, _ = annotator
    unread = _row(user.id)
    db_session.add(unread)
    await db_session.flush()
    unread_id = unread.id

    nested = await db_session.begin_nested()
    unread.read_at = datetime.now(timezone.utc)
    with db_session.no_autoflush:
        deleted = await NotificationService(db_session).clear_read(user.id)
        after_delete = {
            "deleted": deleted,
            "row": _state(unread, db_session),
            "in_database": await _exists(db_session, unread_id),
        }
    await nested.rollback()
    after_rollback = {
        "row": _state(unread, db_session),
        "in_database": await _exists(db_session, unread_id),
    }
    print(
        "legacy_clear_read_no_autoflush_dirty_read_false_positive",
        json.dumps(after_delete, sort_keys=True),
    )
    print(
        "legacy_clear_read_no_autoflush_dirty_read_false_positive_rollback",
        json.dumps(after_rollback, sort_keys=True),
    )
    assert deleted == 0
    assert after_delete["in_database"] is True
    assert after_rollback["in_database"] is True


@pytest.mark.asyncio
async def test_legacy_delete_for_user_no_autoflush_dirty_user_id_baseline(
    db_session, annotator, reviewer
):
    user, _ = annotator
    other_user, _ = reviewer
    target = _row(user.id)
    db_session.add(target)
    await db_session.flush()
    target_id = target.id

    nested = await db_session.begin_nested()
    target.user_id = other_user.id
    with db_session.no_autoflush:
        deleted = await NotificationService(db_session).delete_for_user(
            user.id, target_id
        )
        after_delete = {
            "deleted": deleted,
            "row": _state(target, db_session),
            "in_database": await _exists(db_session, target_id),
        }
    await nested.rollback()
    after_rollback = {
        "row": _state(target, db_session),
        "in_database": await _exists(db_session, target_id),
        "database_user_is_original": await db_session.scalar(
            select(Notification.user_id).where(Notification.id == target_id)
        )
        == user.id,
    }
    print(
        "legacy_delete_for_user_no_autoflush_dirty_user",
        json.dumps(after_delete, sort_keys=True),
    )
    print(
        "legacy_delete_for_user_no_autoflush_dirty_user_rollback",
        json.dumps(after_rollback, sort_keys=True),
    )
    assert deleted is True
    assert after_delete["in_database"] is False
    assert after_rollback["in_database"] is True
    assert after_rollback["database_user_is_original"] is True


@pytest.mark.asyncio
async def test_legacy_delete_for_user_no_autoflush_dirty_user_false_positive_baseline(
    db_session, annotator, reviewer
):
    user, _ = annotator
    other_user, _ = reviewer
    target = _row(other_user.id)
    db_session.add(target)
    await db_session.flush()
    target_id = target.id

    nested = await db_session.begin_nested()
    target.user_id = user.id
    with db_session.no_autoflush:
        deleted = await NotificationService(db_session).delete_for_user(
            user.id, target_id
        )
        after_delete = {
            "deleted": deleted,
            "row": _state(target, db_session),
            "in_database": await _exists(db_session, target_id),
        }
    await nested.rollback()
    after_rollback = {
        "row": _state(target, db_session),
        "in_database": await _exists(db_session, target_id),
        "database_user_is_original": await db_session.scalar(
            select(Notification.user_id).where(Notification.id == target_id)
        )
        == other_user.id,
    }
    print(
        "legacy_delete_for_user_no_autoflush_dirty_user_false_positive",
        json.dumps(after_delete, sort_keys=True),
    )
    print(
        "legacy_delete_for_user_no_autoflush_dirty_user_false_positive_rollback",
        json.dumps(after_rollback, sort_keys=True),
    )
    assert deleted is False
    assert after_delete["in_database"] is True
    assert after_rollback["database_user_is_original"] is True


@pytest.mark.asyncio
async def test_read_state_owner_matches_legacy_update_with_dirty_user_no_autoflush(
    db_session, annotator, reviewer
):
    user, _ = annotator
    database_user, _ = reviewer
    legacy = _row(database_user.id)
    owner = _row(database_user.id)
    db_session.add_all([legacy, owner])
    await db_session.flush()
    legacy_id = legacy.id
    owner_id = owner.id

    nested = await db_session.begin_nested()
    legacy.user_id = user.id
    owner.user_id = user.id
    read_at = datetime.now(timezone.utc)
    with db_session.no_autoflush:
        legacy_result = await db_session.execute(
            update(Notification)
            .where(
                Notification.id == legacy_id,
                Notification.user_id == user.id,
                Notification.read_at.is_(None),
            )
            .values(read_at=read_at)
        )
        legacy_snapshot = {
            "rowcount": legacy_result.rowcount,
            "cached_read_at": sa_inspect(legacy).dict.get("read_at"),
            "cached_state": _state(legacy, db_session),
            "database": (
                await db_session.execute(
                    select(Notification.user_id, Notification.read_at).where(
                        Notification.id == legacy_id
                    )
                )
            )
            .one()
            ._asdict(),
        }
        owner_result = await NotificationService(db_session).mark_read(
            user.id, owner_id
        )
        owner_snapshot = {
            "updated": owner_result,
            "cached_read_at": sa_inspect(owner).dict.get("read_at"),
            "cached_state": _state(owner, db_session),
            "database": (
                await db_session.execute(
                    select(Notification.user_id, Notification.read_at).where(
                        Notification.id == owner_id
                    )
                )
            )
            .one()
            ._asdict(),
        }
    print(
        "legacy_read_state_dirty_user_no_autoflush",
        json.dumps(legacy_snapshot, default=str, sort_keys=True),
    )
    print(
        "owner_read_state_dirty_user_no_autoflush",
        json.dumps(owner_snapshot, default=str, sort_keys=True),
    )
    await nested.rollback()
    assert legacy_result.rowcount == 0
    assert owner_result is False
    assert legacy_snapshot["database"]["user_id"] == database_user.id
    assert owner_snapshot["database"]["user_id"] == database_user.id
    assert legacy_snapshot["database"]["read_at"] is None
    assert owner_snapshot["database"]["read_at"] is None
