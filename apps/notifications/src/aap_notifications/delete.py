"""Portable Notifications deletion capability."""

from __future__ import annotations

from typing import Any, Protocol
import uuid

from .delete_contract import (
    ClearReadData,
    ClearReadRequest,
    DeleteOneData,
    DeleteOneRequest,
    DeleteRequest,
    NotificationDeleteUnavailable,
    decode_delete_reply,
)


class NotificationDeleteData(Protocol):
    async def command(self, request: DeleteRequest) -> Any: ...


class NotificationDeleteManager:
    def __init__(self, data: NotificationDeleteData):
        self.data = data

    async def delete_for_user(
        self, user_id: uuid.UUID, notification_id: uuid.UUID
    ) -> DeleteOneData:
        reply = decode_delete_reply(
            await self.data.command(
                DeleteOneRequest(
                    schema="aap-notification-delete/v1",
                    operation="delete_one",
                    user_id=user_id,
                    notification_id=notification_id,
                )
            )
        )
        if reply.data.operation == "delete_one":
            return reply.data
        raise NotificationDeleteUnavailable()

    async def clear_read(self, user_id: uuid.UUID) -> ClearReadData:
        reply = decode_delete_reply(
            await self.data.command(
                ClearReadRequest(
                    schema="aap-notification-delete/v1",
                    operation="clear_read",
                    user_id=user_id,
                )
            )
        )
        if reply.data.operation == "clear_read":
            return reply.data
        raise NotificationDeleteUnavailable()
