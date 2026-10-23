"""Portable wire shapes for immediate Notifications Redis events."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    TypeAdapter,
    field_validator,
)

NOTIFICATION_REALTIME_PROTOCOL = "aap-notification-realtime/v1"
_UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_DATE_TIME_PATTERN = (
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T"
    r"([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]+)?"
    r"(Z|([+-])([0-9]{2}):([0-9]{2}))$"
)


class NotificationEvent(BaseModel):
    """Persisted notification event sent through the existing user channel."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: StrictStr = Field(
        min_length=36,
        max_length=36,
        pattern=_UUID_PATTERN,
        json_schema_extra={"format": "uuid"},
    )
    type: StrictStr = Field(max_length=60)
    target_type: StrictStr = Field(max_length=30)
    target_id: StrictStr = Field(
        min_length=36,
        max_length=36,
        pattern=_UUID_PATTERN,
        json_schema_extra={"format": "uuid"},
    )
    payload: Any
    created_at: StrictStr = Field(
        pattern=_DATE_TIME_PATTERN,
        json_schema_extra={"format": "date-time"},
    )

    @field_validator("id", "target_id")
    @classmethod
    def valid_uuid_string(cls, value: str) -> str:
        if re.fullmatch(_UUID_PATTERN, value) is None:
            raise ValueError("notification ids must be UUID strings")
        return value

    @field_validator("created_at")
    @classmethod
    def valid_created_at(cls, value: str) -> str:
        if re.fullmatch(_DATE_TIME_PATTERN, value) is None:
            raise ValueError("created_at must be an RFC 3339 date-time")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("created_at must be a valid RFC 3339 date-time") from None
        return value


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
    payload: Any,
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
