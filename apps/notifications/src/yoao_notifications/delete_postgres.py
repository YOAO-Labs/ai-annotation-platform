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
    def __init__(
        self,
        session: NotificationDeleteSession,
        *,
        include_migration_inventory: bool = True,
    ):
        self.session = session
        self._function = (
            "delete_command" if include_migration_inventory else "delete_command_scoped"
        )

    async def command(self, request: DeleteRequest) -> Any:
        if self.session.autoflush:
            await self.session.flush()
        return await self.session.scalar(
            text(
                f"SELECT aap_notifications_v1.{self._function}("
                "CAST(:request AS pg_catalog.jsonb))"
            ),
            {"request": request.model_dump_json(by_alias=True)},
        )
