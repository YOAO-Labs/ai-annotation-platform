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
    def __init__(self, session: NotificationReadStateSession):
        self.session = session

    async def command(self, request: ReadStateRequest) -> Any:
        if self.session.autoflush:
            await self.session.flush()
        return await self.session.scalar(
            text(
                "SELECT aap_notifications_v1.read_state_command("
                "CAST(:request AS pg_catalog.jsonb))"
            ),
            {"request": request.model_dump_json(by_alias=True)},
        )
