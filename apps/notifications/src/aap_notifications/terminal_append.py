"""Portable idempotent append for user-visible async-job terminal notifications."""

from __future__ import annotations

from typing import Any, Protocol

from .terminal_append_contract import (
    NotificationTerminalAppendUnavailable,
    TerminalNotificationData,
    TerminalNotificationRequest,
    decode_terminal_append_reply,
)


class NotificationTerminalAppendData(Protocol):
    async def command(self, request: TerminalNotificationRequest) -> Any: ...


class NotificationTerminalAppendManager:
    def __init__(self, data: NotificationTerminalAppendData):
        self.data = data

    async def append(
        self, request: TerminalNotificationRequest
    ) -> TerminalNotificationData:
        data = decode_terminal_append_reply(await self.data.command(request)).data
        notification = data.notification
        if (
            notification.user_id != request.user_id
            or notification.type != request.type
            or notification.target_type != "async_job"
            or notification.target_id != request.target_id
            or (
                data.created
                and (
                    notification.id != request.id
                    or notification.payload != request.payload
                    or notification.read_at is not None
                )
            )
        ):
            raise NotificationTerminalAppendUnavailable()
        return data
