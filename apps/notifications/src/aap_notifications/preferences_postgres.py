"""SQLAlchemy binding for the versioned Notifications PostgreSQL function."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import text

from .preferences_contract import PreferenceRequest


class NotificationPreferenceSession(Protocol):
    autoflush: bool

    async def flush(self) -> None: ...

    async def scalar(self, statement, parameters=None) -> Any: ...


class NotificationPreferencesPostgresData:
    def __init__(self, session: NotificationPreferenceSession):
        self.session = session

    async def command(self, request: PreferenceRequest) -> Any:
        if self.session.autoflush:
            await self.session.flush()
        # The API keeps this transaction and publishes sync only after commit.
        return await self.session.scalar(
            text(
                "SELECT aap_notifications_v1.preference_command("
                "CAST(:request AS jsonb))"
            ),
            {"request": request.model_dump_json(by_alias=True, exclude_none=True)},
        )
