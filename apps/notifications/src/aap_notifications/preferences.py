"""Portable preference projection over the Notifications owner capability."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from .preferences_contract import (
    GetPreferencesRequest,
    IsInAppMutedRequest,
    NotificationPreferenceUnknownType,
    NotificationPreferencesUnavailable,
    PatchPreferenceRequest,
    decode_get_preferences_reply,
    decode_is_in_app_muted_reply,
    decode_patch_preference_reply,
)
from .preferences_policy import (
    KNOWN_NOTIFICATION_TYPES,
    default_toast_for,
)


class NotificationPreferenceData(Protocol):
    async def command(self, request) -> Any: ...


class NotificationPreferenceManager:
    def __init__(self, data: NotificationPreferenceData):
        self.data = data

    async def get(self, user_id: uuid.UUID) -> list[dict[str, Any]]:
        reply = decode_get_preferences_reply(
            await self.data.command(
                GetPreferencesRequest(
                    schema="aap-notification-preferences/v1",
                    operation="get",
                    user_id=user_id,
                )
            )
        )
        return [item.model_dump() for item in reply.data.items]

    async def patch(
        self,
        user_id: uuid.UUID,
        notification_type: str,
        patch: Mapping[str, bool],
    ) -> None:
        request = PatchPreferenceRequest(
            schema="aap-notification-preferences/v1",
            operation="patch",
            user_id=user_id,
            type=notification_type,
            patch=dict(patch),
        )
        decode_patch_preference_reply(
            await self.data.command(request), notification_type
        )

    async def is_in_app_muted(self, user_id: uuid.UUID, notification_type: str) -> bool:
        reply = decode_is_in_app_muted_reply(
            await self.data.command(
                IsInAppMutedRequest(
                    schema="aap-notification-preferences/v1",
                    operation="is_in_app_muted",
                    user_id=user_id,
                    type=notification_type,
                )
            )
        )
        return reply.data.muted


__all__ = [
    "KNOWN_NOTIFICATION_TYPES",
    "NotificationPreferenceManager",
    "NotificationPreferenceUnknownType",
    "NotificationPreferencesUnavailable",
    "default_toast_for",
]
