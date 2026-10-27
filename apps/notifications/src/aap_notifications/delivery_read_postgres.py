"""Notifications fixed reads on the caller's existing SQLAlchemy session."""

from __future__ import annotations

import json
from typing import Any, Protocol

from sqlalchemy import select, text


class NotificationDeliveryReadSession(Protocol):
    autoflush: bool

    async def flush(self) -> None: ...

    async def execute(self, statement, params=None) -> Any: ...

    async def scalar(self, statement, params=None) -> Any: ...


class NotificationDeliveryReadPostgresData:
    def __init__(self, session: NotificationDeliveryReadSession, notification_model):
        self.session = session
        self.notification_model = notification_model

    async def read_notification(self, notification_id):
        result = await self.session.execute(
            select(self.notification_model).from_statement(
                text(
                    "SELECT * FROM aap_notifications_v1.read_notification("
                    "CAST(:notification_id AS pg_catalog.uuid))"
                )
            ),
            {"notification_id": notification_id},
        )
        return result.scalar_one_or_none()

    async def find_terminal_notification(self, user_id, notification_type, target_id):
        return await self.session.scalar(
            select(
                self.notification_model.id.label("find_terminal_notification")
            ).from_statement(
                text(
                    "SELECT aap_notifications_v1.find_terminal_notification("
                    "CAST(:user_id AS pg_catalog.uuid), "
                    "CAST(:notification_type AS pg_catalog.text), "
                    "CAST(:target_id AS pg_catalog.uuid))"
                )
            ),
            {
                "user_id": user_id,
                "notification_type": notification_type,
                "target_id": target_id,
            },
        )

    async def job_scopes(self, job_ids):
        if self.session.autoflush:
            await self.session.flush()
        result = await self.session.execute(
            text(
                "SELECT id, scope_project_id, kind "
                "FROM aap_notifications_v1.delivery_job_scopes("
                "CAST(:job_ids AS pg_catalog.uuid[]))"
            ),
            {"job_ids": list(job_ids)},
        )
        return result.all()

    async def _pairs(self, function_name, pairs):
        # The ORM projection retains each original authorization read's autoflush.
        statement = select(
            self.notification_model.target_id.label("project_id"),
            self.notification_model.user_id.label("user_id"),
        ).from_statement(
            text(
                "SELECT project_id, user_id FROM aap_notifications_v1."
                + function_name
                + "(CAST(:pairs AS pg_catalog.jsonb))"
            )
        )
        result = await self.session.execute(
            statement,
            {
                "pairs": json.dumps(
                    [
                        {"project_id": str(project_id), "user_id": str(user_id)}
                        for project_id, user_id in pairs
                    ]
                )
            },
        )
        return set(result.all())

    async def member_pairs(self, pairs):
        return await self._pairs("delivery_member_pairs", pairs)

    async def reviewer_pairs(self, pairs):
        return await self._pairs("delivery_reviewer_pairs", pairs)

    async def owner_pairs(self, pairs):
        return await self._pairs("delivery_owner_pairs", pairs)

    async def super_pairs(self, pairs):
        return await self._pairs("delivery_super_pairs", pairs)

    async def global_super_admins(self, user_ids):
        if not user_ids:
            return set()
        if self.session.autoflush:
            await self.session.flush()
        result = await self.session.execute(
            text(
                "SELECT actor.id FROM aap_identity_v1.account_profiles AS actor "
                "WHERE actor.id = ANY(CAST(:user_ids AS pg_catalog.uuid[])) "
                "AND actor.is_active IS TRUE AND actor.role = 'super_admin'"
            ),
            {"user_ids": [str(user_id) for user_id in user_ids]},
        )
        return {row[0] for row in result.all()}
