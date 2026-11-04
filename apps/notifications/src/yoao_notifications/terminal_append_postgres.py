"""Borrowed SQLAlchemy session binding for the atomic terminal append function."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import text

from .terminal_append_contract import TerminalNotificationRequest


class NotificationTerminalAppendSession(Protocol):
    async def scalar(self, statement, parameters=None) -> Any: ...


class NotificationTerminalAppendPostgresData:
    def __init__(self, session: NotificationTerminalAppendSession):
        self.session = session

    async def command(self, request: TerminalNotificationRequest) -> Any:
        return await self.session.scalar(
            text(
                "SELECT aap_notifications_v1.append_job_terminal_notification("
                "CAST(:request AS pg_catalog.jsonb))"
            ),
            {"request": request.model_dump_json(by_alias=True)},
        )
