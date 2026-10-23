"""Portable wire shapes for immediate Notifications Redis events."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, TypeAdapter

NOTIFICATION_REALTIME_PROTOCOL = "aap-notification-realtime/v1"


class NotificationEvent(BaseModel):
    """Persisted notification event sent through the existing user channel."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: StrictStr = Field(json_schema_extra={"format": "uuid"})
    type: StrictStr = Field(max_length=60)
    target_type: StrictStr = Field(max_length=30)
    target_id: StrictStr = Field(json_schema_extra={"format": "uuid"})
    payload: dict[str, Any]
    created_at: StrictStr = Field(json_schema_extra={"format": "date-time"})


class NotificationSyncEvent(BaseModel):
    """Open sync hint; clients may ignore unknown reasons and extensions."""

    model_config = ConfigDict(extra="allow", strict=True, frozen=True)

    type: Literal["notifications.sync"]
    reason: StrictStr | None = None


NotificationRealtimeEvent = NotificationEvent | NotificationSyncEvent
_EVENT_ADAPTER = TypeAdapter(NotificationRealtimeEvent)


def notification_event_message(
    *,
    id: str,
    type: str,
    target_type: str,
    target_id: str,
    payload: dict[str, Any],
    created_at: str,
) -> dict[str, Any]:
    """Build the existing event fields without converting caller values."""

    return NotificationEvent(
        id=id,
        type=type,
        target_type=target_type,
        target_id=target_id,
        payload=payload,
        created_at=created_at,
    ).model_dump(mode="python")


def notification_sync_message(*, reason: str) -> dict[str, Any]:
    """Build the existing sync hint without adding a new envelope."""

    return NotificationSyncEvent(
        type="notifications.sync",
        reason=reason,
    ).model_dump(mode="python", exclude_unset=True)


def notification_realtime_json_schema() -> dict[str, Any]:
    schema = _EVENT_ADAPTER.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = NOTIFICATION_REALTIME_PROTOCOL
    schema["title"] = "AAP Notifications realtime event protocol"
    return schema
