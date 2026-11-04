"""Portable Notifications read-state capability."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
import uuid

from .read_state_contract import (
    MarkAllReadData,
    MarkAllReadRequest,
    MarkReadData,
    MarkReadRequest,
    NotificationReadStateUnavailable,
    ReadStateRequest,
    decode_read_state_reply,
)


class NotificationReadStateData(Protocol):
    async def command(self, request: ReadStateRequest) -> Any: ...


class NotificationReadStateManager:
    def __init__(self, data: NotificationReadStateData):
        self.data = data

    async def mark_read(
        self, user_id: uuid.UUID, notification_id: uuid.UUID, read_at: datetime
    ) -> MarkReadData:
        reply = decode_read_state_reply(
            await self.data.command(
                MarkReadRequest(
                    schema="aap-notification-read-state/v1",
                    operation="mark_read",
                    user_id=user_id,
                    notification_id=notification_id,
                    read_at=read_at,
                )
            )
        )
        if reply.data.operation == "mark_read":
            expected_ids = [notification_id] if reply.data.updated else []
            if reply.data.updated_ids != expected_ids:
                raise NotificationReadStateUnavailable()
            return reply.data
        raise NotificationReadStateUnavailable()

    async def mark_all_read(
        self, user_id: uuid.UUID, read_at: datetime
    ) -> MarkAllReadData:
        reply = decode_read_state_reply(
            await self.data.command(
                MarkAllReadRequest(
                    schema="aap-notification-read-state/v1",
                    operation="mark_all_read",
                    user_id=user_id,
                    read_at=read_at,
                )
            )
        )
        if reply.data.operation == "mark_all_read":
            return reply.data
        raise NotificationReadStateUnavailable()
