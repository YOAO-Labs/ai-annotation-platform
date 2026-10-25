from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import inspect
from sqlalchemy.orm import Session, make_transient_to_detached

from app.db.models.notification import Notification
from app.services.notification import NotificationService


def test_read_state_cache_sync_preserves_other_pending_notification_changes():
    notification_id = uuid.uuid4()
    user_id = uuid.uuid4()
    row = Notification(
        id=notification_id,
        user_id=user_id,
        type="read-state.test",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={"stored": True},
        read_at=None,
    )
    make_transient_to_detached(row)
    session = Session()
    session.add(row)
    row.payload = {"pending": True}
    read_at = datetime.now(timezone.utc)

    NotificationService(session)._synchronize_read_state_identity(user_id, read_at)

    assert row.read_at == read_at
    assert row.payload == {"pending": True}
    assert session.is_modified(row, include_collections=False)
    assert not inspect(row).attrs.read_at.history.has_changes()
    session.close()
