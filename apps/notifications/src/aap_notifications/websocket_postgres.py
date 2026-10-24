"""Read-only Notifications capabilities for the WebSocket owner."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import logging
from typing import Any, AsyncIterator, Protocol
import uuid

from .delivery_read import NotificationDeliveryReadPolicy
from .delivery_read_contract import NotificationDeliveryReadFact

log = logging.getLogger("aap_notifications.websocket.postgres")


class NotificationSocketConnection(Protocol):
    async def fetchrow(self, query: str, *args: Any) -> Any: ...

    async def fetch(self, query: str, *args: Any) -> list[Any]: ...

    async def set_type_codec(self, *args: Any, **kwargs: Any) -> None: ...

    def transaction(self, **kwargs: Any) -> Any: ...


class NotificationSocketPool(Protocol):
    def acquire(self) -> Any: ...


async def configure_notification_jsonb_codec(
    connection: NotificationSocketConnection,
) -> None:
    await connection.set_type_codec(
        "jsonb",
        schema="pg_catalog",
        encoder=json.dumps,
        decoder=json.loads,
        format="text",
    )


class NotificationSocketPostgresData:
    """Use fixed delivery-read functions on one borrowed connection."""

    def __init__(self, connection: NotificationSocketConnection):
        self.connection = connection
        self._found: dict[uuid.UUID, NotificationDeliveryReadFact] = {}
        self._policy = NotificationDeliveryReadPolicy(self)

    async def notification_by_id(
        self, notification_id: uuid.UUID
    ) -> NotificationDeliveryReadFact | None:
        cached = self._found.get(notification_id)
        if cached is not None:
            return cached
        record = await self.connection.fetchrow(
            "SELECT * FROM aap_notifications_v1.read_notification("
            "CAST($1 AS pg_catalog.uuid))",
            notification_id,
        )
        if record is None:
            return None
        row = NotificationDeliveryReadFact.model_validate(dict(record))
        self._found[row.id] = row
        return row

    async def notification_deliverable(
        self, notification_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        row = await self.notification_by_id(notification_id)
        if row is None or row.user_id != user_id:
            return False
        try:
            scopes = await self._policy.resolve_scopes([row])
            allowed = await self._policy.allowed_indices([row], scopes)
        except Exception:
            log.warning(
                "notification delivery scope recheck failed notification_id=%s",
                notification_id,
            )
            return False
        return 0 in allowed

    async def job_scopes(self, job_ids: set[uuid.UUID]):
        rows = await self.connection.fetch(
            "SELECT id, scope_project_id, kind "
            "FROM aap_notifications_v1.delivery_job_scopes("
            "CAST($1 AS pg_catalog.uuid[]))",
            list(job_ids),
        )
        return [(row["id"], row["scope_project_id"], row["kind"]) for row in rows]

    async def _pairs(self, function_name: str, pairs):
        rows = await self.connection.fetch(
            f"SELECT project_id, user_id "
            f"FROM aap_notifications_v1.{function_name}("
            f"CAST($1 AS pg_catalog.jsonb))",
            [
                {"project_id": str(project_id), "user_id": str(user_id)}
                for project_id, user_id in pairs
            ],
        )
        return {(row["project_id"], row["user_id"]) for row in rows}

    async def member_pairs(self, pairs):
        return await self._pairs("delivery_member_pairs", pairs)

    async def reviewer_pairs(self, pairs):
        return await self._pairs("delivery_reviewer_pairs", pairs)

    async def owner_pairs(self, pairs):
        return await self._pairs("delivery_owner_pairs", pairs)

    async def super_pairs(self, pairs):
        return await self._pairs("delivery_super_pairs", pairs)


@asynccontextmanager
async def open_notification_socket_data(
    pool: NotificationSocketPool,
) -> AsyncIterator[NotificationSocketPostgresData]:
    async with pool.acquire() as connection:
        transaction = connection.transaction(isolation="read_committed", readonly=True)
        await transaction.start()
        try:
            yield NotificationSocketPostgresData(connection)
        finally:
            await transaction.rollback()
