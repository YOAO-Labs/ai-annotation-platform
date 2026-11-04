"""Borrowed SQLAlchemy session binding for the Notifications read-state function."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import text

from .read_state_contract import ReadStateRequest


class NotificationReadStateSession(Protocol):
    autoflush: bool

    async def flush(self) -> None: ...

    async def scalar(self, statement, parameters=None) -> Any: ...


class NotificationReadStatePostgresData:
    def __init__(
        self,
        session: NotificationReadStateSession,
        *,
        include_migration_inventory: bool = True,
    ):
        self.session = session
        self._function = (
            "read_state_command"
            if include_migration_inventory
            else "read_state_command_scoped"
        )

    async def command(self, request: ReadStateRequest) -> Any:
        if self.session.autoflush:
            await self.session.flush()
        return await self.session.scalar(
            text(
                f"SELECT aap_notifications_v1.{self._function}("
                "CAST(:request AS pg_catalog.jsonb))"
            ),
            {"request": request.model_dump_json(by_alias=True)},
        )
