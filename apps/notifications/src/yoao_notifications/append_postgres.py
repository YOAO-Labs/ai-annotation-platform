"""Borrowed SQLAlchemy session binding for the versioned append function."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import text

from .append_contract import AppendNotificationRequest


class NotificationAppendSession(Protocol):
    async def scalar(self, statement, parameters=None) -> Any: ...


class NotificationAppendPostgresData:
    def __init__(self, session: NotificationAppendSession):
        self.session = session

    async def command(self, request: AppendNotificationRequest) -> Any:
        return await self.session.scalar(
            text(
                "SELECT aap_notifications_v1.append_notification("
                "CAST(:request AS jsonb))"
            ),
            {"request": request.model_dump_json(by_alias=True, exclude_none=True)},
        )
