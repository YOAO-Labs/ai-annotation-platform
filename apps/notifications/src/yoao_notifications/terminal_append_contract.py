"""Closed wire contract for idempotent async-job terminal notification appends."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .append_contract import AppendedNotification

TERMINAL_APPEND_PROTOCOL = "aap-notification-terminal-append/v1"
TERMINAL_NOTIFICATION_TYPES = (
    "job.completed",
    "job.failed",
    "job.cancelled",
)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class TerminalNotificationRequest(_ClosedModel):
    protocol: Literal["aap-notification-terminal-append/v1"] = Field(alias="schema")
    operation: Literal["append_job_terminal"]
    id: uuid.UUID
    user_id: uuid.UUID
    type: Literal["job.completed", "job.failed", "job.cancelled"]
    target_id: uuid.UUID
    payload: dict[str, Any]

    @field_validator("payload")
    @classmethod
    def finite_json_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("payload must contain finite JSON values") from None
        return value

    @model_validator(mode="after")
    def status_matches_type(self) -> TerminalNotificationRequest:
        if self.payload.get("status") != self.type.removeprefix("job."):
            raise ValueError("terminal payload status must match notification type")
        return self


class TerminalNotificationData(_ClosedModel):
    operation: Literal["append_job_terminal"]
    created: StrictBool
    notification: AppendedNotification


class TerminalNotificationReply(_ClosedModel):
    protocol: Literal["aap-notification-terminal-append/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: TerminalNotificationData


class TerminalAppendError(_ClosedModel):
    code: Literal["invalid_request", "unsupported_isolation"]


class TerminalNotificationFailure(_ClosedModel):
    protocol: Literal["aap-notification-terminal-append/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: TerminalAppendError


TerminalNotificationMessage: TypeAlias = (
    TerminalNotificationRequest
    | TerminalNotificationReply
    | TerminalNotificationFailure
)
_MESSAGE_ADAPTER = TypeAdapter(TerminalNotificationMessage)


class NotificationTerminalAppendUnavailable(RuntimeError):
    """The Notifications terminal-append capability returned an invalid reply."""


def _reply_object(value: Any) -> Mapping[str, Any]:
    try:
        if isinstance(value, (str, bytes, bytearray)):
            value = json.loads(value)
        if not isinstance(value, Mapping):
            raise ValueError("reply must be an object")
        return value
    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError):
        raise NotificationTerminalAppendUnavailable() from None


def decode_terminal_append_reply(value: Any) -> TerminalNotificationReply:
    raw = _reply_object(value)
    try:
        if raw.get("ok") is False:
            TerminalNotificationFailure.model_validate(raw)
            raise NotificationTerminalAppendUnavailable()
        return TerminalNotificationReply.model_validate(raw)
    except (ValidationError, ValueError, TypeError):
        raise NotificationTerminalAppendUnavailable() from None


def terminal_append_json_schema() -> dict[str, Any]:
    schema = _MESSAGE_ADAPTER.json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = TERMINAL_APPEND_PROTOCOL
    schema["title"] = "AAP async-job terminal notification append protocol"
    return schema
