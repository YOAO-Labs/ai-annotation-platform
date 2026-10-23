"""SQL bindings to Notifications' fixed public fact relation."""

from __future__ import annotations

from sqlalchemy import DateTime, String, column, table
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import aliased

from app.db.models.notification import Notification


def notification_facts():
    return table(
        "notification_facts",
        column("id", UUID(as_uuid=True)),
        column("user_id", UUID(as_uuid=True)),
        column("type", String(60)),
        column("target_type", String(30)),
        column("target_id", UUID(as_uuid=True)),
        column("payload", JSONB),
        column("read_at", DateTime(timezone=True)),
        column("created_at", DateTime(timezone=True)),
        schema="aap_notifications_v1",
    ).alias("notifications_notification_facts")


def notification_rows():
    return aliased(Notification, notification_facts(), adapt_on_names=True)
