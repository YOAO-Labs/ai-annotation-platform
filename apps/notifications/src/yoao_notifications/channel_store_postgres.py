"""PostgreSQL owner-function adapter for external notification channels and outbox."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import text


class _AsyncSqlSession(Protocol):
    async def scalar(
        self, statement: Any, params: dict[str, Any] | None = None
    ) -> Any: ...


def _decode_json(raw: Any) -> Any:
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _encode_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


class NotificationChannelStorePostgresData:
    """Thin SQLAlchemy async wrapper over yoao_notifications_v1 channel functions."""

    def __init__(self, session: _AsyncSqlSession) -> None:
        self.session = session

    async def list_channels(
        self, *, include_retired: bool = False
    ) -> list[dict[str, Any]]:
        raw = await self.session.scalar(
            text(
                "SELECT yoao_notifications_v1.list_notification_channels("
                "CAST(:include_retired AS pg_catalog.boolean))"
            ),
            {"include_retired": include_retired},
        )
        decoded = _decode_json(raw)
        return list(decoded) if isinstance(decoded, list) else []

    async def get_channel(self, channel_id: str) -> dict[str, Any] | None:
        raw = await self.session.scalar(
            text(
                "SELECT yoao_notifications_v1.get_notification_channel("
                "CAST(:channel_id AS pg_catalog.text))"
            ),
            {"channel_id": channel_id},
        )
        decoded = _decode_json(raw)
        return decoded if isinstance(decoded, dict) else None

    async def register_channel(self, request: dict[str, Any]) -> dict[str, Any]:
        raw = await self.session.scalar(
            text(
                "SELECT yoao_notifications_v1.register_notification_channel("
                "CAST(:payload AS pg_catalog.jsonb))"
            ),
            {"payload": _encode_json(request)},
        )
        decoded = _decode_json(raw)
        return (
            decoded
            if isinstance(decoded, dict)
            else {"ok": False, "error": "invalid_reply"}
        )

    async def transition_channel(self, request: dict[str, Any]) -> dict[str, Any]:
        raw = await self.session.scalar(
            text(
                "SELECT yoao_notifications_v1.transition_notification_channel("
                "CAST(:payload AS pg_catalog.jsonb))"
            ),
            {"payload": _encode_json(request)},
        )
        decoded = _decode_json(raw)
        return (
            decoded
            if isinstance(decoded, dict)
            else {"ok": False, "error": "invalid_reply"}
        )

    async def open_channel_secret(self, channel_id: str) -> dict[str, Any] | None:
        raw = await self.session.scalar(
            text(
                "SELECT yoao_notifications_v1.open_notification_channel_secret("
                "CAST(:channel_id AS pg_catalog.text))"
            ),
            {"channel_id": channel_id},
        )
        decoded = _decode_json(raw)
        return decoded if isinstance(decoded, dict) else None

    async def list_user_bindings(self, user_id: uuid.UUID) -> list[dict[str, Any]]:
        raw = await self.session.scalar(
            text(
                "SELECT yoao_notifications_v1.list_user_channel_bindings("
                "CAST(:user_id AS pg_catalog.uuid))"
            ),
            {"user_id": str(user_id)},
        )
        decoded = _decode_json(raw)
        return list(decoded) if isinstance(decoded, list) else []

    async def upsert_user_binding(self, request: dict[str, Any]) -> dict[str, Any]:
        raw = await self.session.scalar(
            text(
                "SELECT yoao_notifications_v1.upsert_user_channel_binding("
                "CAST(:payload AS pg_catalog.jsonb))"
            ),
            {"payload": _encode_json(request)},
        )
        decoded = _decode_json(raw)
        return (
            decoded
            if isinstance(decoded, dict)
            else {"ok": False, "error": "invalid_reply"}
        )

    async def mark_user_binding_verified(
        self,
        user_id: uuid.UUID,
        channel_id: str,
        verified_at: datetime | None = None,
    ) -> dict[str, Any]:
        raw = await self.session.scalar(
            text(
                "SELECT yoao_notifications_v1.mark_user_channel_binding_verified("
                "CAST(:user_id AS pg_catalog.uuid), "
                "CAST(:channel_id AS pg_catalog.text), "
                "CAST(:verified_at AS pg_catalog.timestamptz))"
            ),
            {
                "user_id": str(user_id),
                "channel_id": channel_id,
                "verified_at": verified_at,
            },
        )
        decoded = _decode_json(raw)
        return (
            decoded
            if isinstance(decoded, dict)
            else {"ok": False, "error": "invalid_reply"}
        )

    async def delete_user_binding(self, user_id: uuid.UUID, channel_id: str) -> bool:
        raw = await self.session.scalar(
            text(
                "SELECT yoao_notifications_v1.delete_user_channel_binding("
                "CAST(:user_id AS pg_catalog.uuid), "
                "CAST(:channel_id AS pg_catalog.text))"
            ),
            {"user_id": str(user_id), "channel_id": channel_id},
        )
        decoded = _decode_json(raw)
        return bool(isinstance(decoded, dict) and decoded.get("deleted"))

    async def enqueue_deliveries(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        raw = await self.session.scalar(
            text(
                "SELECT yoao_notifications_v1.enqueue_channel_deliveries("
                "CAST(:payload AS pg_catalog.jsonb))"
            ),
            {"payload": _encode_json(request)},
        )
        decoded = _decode_json(raw)
        if not isinstance(decoded, dict) or decoded.get("ok") is not True:
            raise RuntimeError("enqueue_channel_deliveries rejected request")
        enqueued = decoded.get("enqueued")
        return list(enqueued) if isinstance(enqueued, list) else []

    async def has_terminal_delivery(
        self,
        user_id: uuid.UUID,
        notification_type: str,
        target_id: uuid.UUID,
    ) -> bool:
        raw = await self.session.scalar(
            text(
                "SELECT yoao_notifications_v1.has_terminal_channel_delivery("
                "CAST(:user_id AS pg_catalog.uuid), "
                "CAST(:notification_type AS pg_catalog.text), "
                "CAST(:target_id AS pg_catalog.uuid))"
            ),
            {
                "user_id": str(user_id),
                "notification_type": notification_type,
                "target_id": str(target_id),
            },
        )
        return bool(raw)
