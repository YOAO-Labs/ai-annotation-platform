"""Closed wire contract for the Notifications append capability."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
)

APPEND_PROTOCOL = "aap-notification-append/v1"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AppendNotificationRequest(_ClosedModel):
    protocol: Literal["aap-notification-append/v1"] = Field(alias="schema")
    operation: Literal["append"]
    id: uuid.UUID
    user_id: uuid.UUID
    type: StrictStr = Field(min_length=1, max_length=60)
    target_type: StrictStr = Field(max_length=30)
    target_id: uuid.UUID
    payload: dict[str, Any]

    @field_validator("payload")
    @classmethod
    def json_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("payload must contain finite JSON values") from None
        return value

    @field_validator("type")
    @classmethod
    def no_control_characters(cls, value: str) -> str:
        if any(ord(char) < 0x20 for char in value):
            raise ValueError("notification type must not contain control characters")
        return value


class AppendedNotification(_ClosedModel):
    id: uuid.UUID = Field(strict=False)
    user_id: uuid.UUID = Field(strict=False)
    type: StrictStr
    target_type: StrictStr
    target_id: uuid.UUID = Field(strict=False)
    payload: dict[str, Any]
    read_at: datetime | None
    created_at: datetime = Field(strict=False)

    @field_validator("read_at", "created_at", mode="before")
    @classmethod
    def parse_timestamp(cls, value: Any) -> Any:
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @field_validator("created_at")
    @classmethod
    def utc_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("created_at must be UTC-aware")
        return value


class AppendNotificationData(_ClosedModel):
    operation: Literal["append"]
    notification: AppendedNotification


class AppendNotificationReply(_ClosedModel):
    protocol: Literal["aap-notification-append/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: AppendNotificationData


class AppendError(_ClosedModel):
    code: Literal["invalid_request"]


class AppendFailure(_ClosedModel):
    protocol: Literal["aap-notification-append/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: AppendError


AppendMessage: TypeAlias = (
    AppendNotificationRequest | AppendNotificationReply | AppendFailure
)
_MESSAGE_ADAPTER = TypeAdapter(AppendMessage)


class NotificationAppendUnavailable(RuntimeError):
    """The Notifications append capability returned an invalid reply."""


def _reply_object(value: Any) -> Mapping[str, Any]:
    try:
        if isinstance(value, (str, bytes, bytearray)):
            value = json.loads(value)
        if not isinstance(value, Mapping):
            raise ValueError("reply must be an object")
        return value
    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError):
        raise NotificationAppendUnavailable() from None


def decode_append_reply(value: Any) -> AppendNotificationReply:
    raw = _reply_object(value)
    try:
        if raw.get("ok") is False:
            AppendFailure.model_validate(raw)
            raise NotificationAppendUnavailable()
        return AppendNotificationReply.model_validate(raw)
    except (ValidationError, ValueError, TypeError):
        raise NotificationAppendUnavailable() from None


def append_json_schema() -> dict[str, Any]:
    schema = _MESSAGE_ADAPTER.json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = APPEND_PROTOCOL
    schema["title"] = "AAP notification append protocol"
    return schema
