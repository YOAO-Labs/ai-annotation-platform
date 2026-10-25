"""Borrowed SQLAlchemy session binding for Notifications deletion commands."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import text

from .delete_contract import DeleteRequest


class NotificationDeleteSession(Protocol):
    autoflush: bool

    async def flush(self) -> None: ...

    async def scalar(self, statement, parameters=None) -> Any: ...


class NotificationDeletePostgresData:
    def __init__(self, session: NotificationDeleteSession):
        self.session = session

    async def command(self, request: DeleteRequest) -> Any:
        if self.session.autoflush:
            await self.session.flush()
        return await self.session.scalar(
            text(
                "SELECT aap_notifications_v1.delete_command("
                "CAST(:request AS pg_catalog.jsonb))"
            ),
            {"request": request.model_dump_json(by_alias=True)},
        )
