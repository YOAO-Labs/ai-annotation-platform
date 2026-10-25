"""Closed wire contract for notification read-state commands."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, TypeAlias, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

READ_STATE_PROTOCOL = "aap-notification-read-state/v1"
_ReplyUUID: TypeAlias = Annotated[uuid.UUID, Field(strict=False)]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _ReadStateRequest(_ClosedModel):
    protocol: Literal["aap-notification-read-state/v1"] = Field(alias="schema")
    user_id: uuid.UUID
    read_at: datetime

    @field_validator("read_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("read_at must be UTC-aware")
        return value


class MarkReadRequest(_ReadStateRequest):
    operation: Literal["mark_read"]
    notification_id: uuid.UUID


class MarkAllReadRequest(_ReadStateRequest):
    operation: Literal["mark_all_read"]


ReadStateRequest: TypeAlias = Annotated[
    Union[MarkReadRequest, MarkAllReadRequest], Field(discriminator="operation")
]


class MarkReadData(_ClosedModel):
    operation: Literal["mark_read"]
    updated: StrictBool
    updated_ids: list[_ReplyUUID]

    @model_validator(mode="after")
    def receipt_matches_update(self) -> MarkReadData:
        if len(self.updated_ids) != int(self.updated):
            raise ValueError("mark_read receipt does not match its result")
        return self


class MarkAllReadData(_ClosedModel):
    operation: Literal["mark_all_read"]
    updated: Annotated[StrictInt, Field(ge=0)]
    updated_ids: list[_ReplyUUID]

    @model_validator(mode="after")
    def receipt_matches_count(self) -> MarkAllReadData:
        if self.updated != len(self.updated_ids) or len(set(self.updated_ids)) != len(
            self.updated_ids
        ):
            raise ValueError("mark_all_read receipt does not match its count")
        return self


class _ReadStateReplyBase(_ClosedModel):
    protocol: Literal["aap-notification-read-state/v1"] = Field(alias="schema")
    ok: Literal[True]


class MarkReadReply(_ReadStateReplyBase):
    data: MarkReadData


class MarkAllReadReply(_ReadStateReplyBase):
    data: MarkAllReadData


ReadStateReply: TypeAlias = Union[MarkReadReply, MarkAllReadReply]
_REPLY_ADAPTER = TypeAdapter(ReadStateReply)


class ReadStateError(_ClosedModel):
    code: Literal["invalid_request"]


class ReadStateFailure(_ClosedModel):
    protocol: Literal["aap-notification-read-state/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: ReadStateError


ReadStateMessage: TypeAlias = Union[
    MarkReadRequest,
    MarkAllReadRequest,
    MarkReadReply,
    MarkAllReadReply,
    ReadStateFailure,
]
_MESSAGE_ADAPTER = TypeAdapter(ReadStateMessage)


class NotificationReadStateUnavailable(RuntimeError):
    """The Notifications read-state capability returned an invalid reply."""


def _reply_object(value: Any) -> Mapping[str, Any]:
    try:
        if isinstance(value, (str, bytes, bytearray)):
            value = json.loads(value)
        if not isinstance(value, Mapping):
            raise ValueError("reply must be an object")
        return value
    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError):
        raise NotificationReadStateUnavailable() from None


def decode_read_state_reply(value: Any) -> ReadStateReply:
    raw = _reply_object(value)
    if raw.get("ok") is False:
        try:
            ReadStateFailure.model_validate(raw)
        except (ValidationError, ValueError, TypeError):
            raise NotificationReadStateUnavailable() from None
        raise NotificationReadStateUnavailable()
    try:
        return _REPLY_ADAPTER.validate_python(raw)
    except (ValidationError, ValueError, TypeError):
        raise NotificationReadStateUnavailable() from None


def read_state_json_schema() -> dict[str, Any]:
    schema = _MESSAGE_ADAPTER.json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = READ_STATE_PROTOCOL
    schema["title"] = "AAP notification read-state protocol"
    return schema
