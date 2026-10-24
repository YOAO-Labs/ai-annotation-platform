"""Borrowed SQLAlchemy session binding for the REST inbox query capability."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import func, select, text


class NotificationInboxQuerySession(Protocol):
    async def execute(self, statement, params=None) -> Any: ...

    async def scalar(self, statement, params=None) -> Any: ...


class NotificationInboxQueryPostgresData:
    """Run fixed functions through ORM statements, preserving autoflush and identity."""

    def __init__(self, session: NotificationInboxQuerySession, notification_model):
        self.session = session
        self.notification_model = notification_model

    async def page(
        self,
        actor_id,
        platform_role: str,
        *,
        unread_only: bool,
        limit: int,
        offset: int,
    ) -> list[Any]:
        statement = select(self.notification_model).from_statement(
            text(
                "SELECT * FROM aap_notifications_v1.inbox_page("
                "CAST(:actor_id AS pg_catalog.uuid), "
                "CAST(:platform_role AS pg_catalog.text), "
                "CAST(:unread_only AS pg_catalog.boolean), "
                "CAST(:limit AS pg_catalog.integer), "
                "CAST(:offset AS pg_catalog.integer))"
            )
        )
        result = await self.session.execute(
            statement,
            {
                "actor_id": actor_id,
                "platform_role": platform_role,
                "unread_only": unread_only,
                "limit": limit,
                "offset": offset,
            },
        )
        return list(result.scalars().all())

    async def total_count(self, actor_id, platform_role: str) -> int:
        statement = select(func.count(self.notification_model.id)).from_statement(
            text(
                "SELECT aap_notifications_v1.inbox_total_count("
                "CAST(:actor_id AS pg_catalog.uuid), "
                "CAST(:platform_role AS pg_catalog.text))"
            )
        )
        value = await self.session.scalar(
            statement,
            {"actor_id": actor_id, "platform_role": platform_role},
        )
        return int(value or 0)

    async def unread_count(self, actor_id, platform_role: str) -> int:
        statement = select(func.count(self.notification_model.id)).from_statement(
            text(
                "SELECT aap_notifications_v1.inbox_unread_count("
                "CAST(:actor_id AS pg_catalog.uuid), "
                "CAST(:platform_role AS pg_catalog.text))"
            )
        )
        value = await self.session.scalar(
            statement,
            {"actor_id": actor_id, "platform_role": platform_role},
        )
        return int(value or 0)
