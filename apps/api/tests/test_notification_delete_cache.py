from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import inspect
from sqlalchemy.orm import Session, make_transient_to_detached

from app.db.models.notification import Notification
from app.services.notification import NotificationService


def _attached_row(user_id: uuid.UUID, *, read_at: datetime | None = None):
    row = Notification(
        id=uuid.uuid4(),
        user_id=user_id,
        type="delete-cache.test",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={},
        read_at=read_at,
    )
    make_transient_to_detached(row)
    session = Session()
    session.add(row)
    return session, row


def test_delete_identity_sync_uses_current_user_predicate_without_loading_rows():
    owner_id = uuid.uuid4()
    other_id = uuid.uuid4()
    session, matching = _attached_row(owner_id)
    other_session, mismatched = _attached_row(owner_id)
    mismatched.user_id = other_id

    NotificationService(session)._synchronize_delete_identity(
        owner_id, notification_id=matching.id
    )
    NotificationService(other_session)._synchronize_delete_identity(
        owner_id, notification_id=mismatched.id
    )

    assert inspect(matching).deleted
    assert matching not in session.identity_map.values()
    assert inspect(mismatched).persistent
    assert mismatched in other_session.identity_map.values()
    session.close()
    other_session.close()


def test_clear_read_delete_identity_sync_preserves_unread_and_expired_rows():
    owner_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    session, read = _attached_row(owner_id, read_at=now)
    unread_session, unread = _attached_row(owner_id)
    expired_session, expired = _attached_row(owner_id, read_at=now)
    expired_session.expire(expired)

    NotificationService(session)._synchronize_delete_identity(owner_id, clear_read=True)
    NotificationService(unread_session)._synchronize_delete_identity(
        owner_id, clear_read=True
    )
    NotificationService(expired_session)._synchronize_delete_identity(
        owner_id, clear_read=True
    )

    assert inspect(read).deleted
    assert inspect(unread).persistent
    assert inspect(expired).persistent and inspect(expired).expired
    session.close()
    unread_session.close()
    expired_session.close()
