"""Persistence commands for user notification preferences."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy import func, literal
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.notification_preference import NotificationPreference
from app.services.notification_preferences import default_toast_for


class NotificationPreferenceCommandService:
    """Own atomic writes to a user's per-type notification channel settings."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def merge_channels(
        self,
        *,
        user_id: uuid.UUID,
        notification_type: str,
        patch: Mapping[str, bool],
    ) -> None:
        """Atomically upsert defaults or merge only the submitted channel keys.

        The conflict update uses PostgreSQL JSONB concatenation so separate
        sessions changing different keys cannot overwrite one another's values.
        Commit remains the caller's responsibility.
        """
        insert_channels = {
            "in_app": True,
            "email": False,
            "toast": default_toast_for(notification_type),
        }
        insert_channels.update(patch)

        merged_channels = NotificationPreference.channels.op("||", return_type=JSONB)(
            literal(patch, JSONB)
        )
        stmt = (
            pg_insert(NotificationPreference)
            .values(
                user_id=user_id,
                type=notification_type,
                channels=insert_channels,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "type"],
                set_={"channels": merged_channels, "updated_at": func.now()},
            )
        )
        await self.db.execute(stmt)
