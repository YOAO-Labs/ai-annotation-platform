"""Portable Notifications append capability."""

from __future__ import annotations

from typing import Any, Protocol

from .append_contract import (
    AppendNotificationData,
    AppendNotificationRequest,
    NotificationAppendUnavailable,
    decode_append_reply,
)


class NotificationAppendData(Protocol):
    async def command(self, request: AppendNotificationRequest) -> Any: ...


class NotificationAppendManager:
    def __init__(self, data: NotificationAppendData):
        self.data = data

    async def append(
        self, request: AppendNotificationRequest
    ) -> AppendNotificationData:
        data = decode_append_reply(await self.data.command(request)).data
        notification = data.notification
        if (
            notification.id != request.id
            or notification.user_id != request.user_id
            or notification.type != request.type
            or notification.target_type != request.target_type
            or notification.target_id != request.target_id
            or notification.payload != request.payload
            or notification.read_at is not None
        ):
            raise NotificationAppendUnavailable()
        return data
