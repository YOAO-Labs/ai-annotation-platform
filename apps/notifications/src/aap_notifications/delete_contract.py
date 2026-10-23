"""Closed wire contract for per-account notification deletion commands."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Annotated, Any, Literal, TypeAlias, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    TypeAdapter,
    ValidationError,
)

DELETE_PROTOCOL = "aap-notification-delete/v1"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class DeleteOneRequest(_ClosedModel):
    protocol: Literal["aap-notification-delete/v1"] = Field(alias="schema")
    operation: Literal["delete_one"]
    user_id: uuid.UUID
    notification_id: uuid.UUID


class ClearReadRequest(_ClosedModel):
    protocol: Literal["aap-notification-delete/v1"] = Field(alias="schema")
    operation: Literal["clear_read"]
    user_id: uuid.UUID


DeleteRequest: TypeAlias = Annotated[
    Union[DeleteOneRequest, ClearReadRequest], Field(discriminator="operation")
]


class DeleteOneData(_ClosedModel):
    operation: Literal["delete_one"]
    deleted: StrictBool


class ClearReadData(_ClosedModel):
    operation: Literal["clear_read"]
    deleted: Annotated[StrictInt, Field(ge=0)]


class _DeleteReplyBase(_ClosedModel):
    protocol: Literal["aap-notification-delete/v1"] = Field(alias="schema")
    ok: Literal[True]


class DeleteOneReply(_DeleteReplyBase):
    data: DeleteOneData


class ClearReadReply(_DeleteReplyBase):
    data: ClearReadData


DeleteReply: TypeAlias = Union[DeleteOneReply, ClearReadReply]
_REPLY_ADAPTER = TypeAdapter(DeleteReply)


class DeleteError(_ClosedModel):
    code: Literal["invalid_request"]


class DeleteFailure(_ClosedModel):
    protocol: Literal["aap-notification-delete/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: DeleteError


DeleteMessage: TypeAlias = Union[
    DeleteOneRequest,
    ClearReadRequest,
    DeleteOneReply,
    ClearReadReply,
    DeleteFailure,
]
_MESSAGE_ADAPTER = TypeAdapter(DeleteMessage)


class NotificationDeleteUnavailable(RuntimeError):
    """The Notifications deletion capability returned an invalid reply."""


def _reply_object(value: Any) -> Mapping[str, Any]:
    try:
        if isinstance(value, (str, bytes, bytearray)):
            value = json.loads(value)
        if not isinstance(value, Mapping):
            raise ValueError("reply must be an object")
        return value
    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError):
        raise NotificationDeleteUnavailable() from None


def decode_delete_reply(value: Any) -> DeleteReply:
    raw = _reply_object(value)
    if raw.get("ok") is False:
        try:
            DeleteFailure.model_validate(raw)
        except (ValidationError, ValueError, TypeError):
            raise NotificationDeleteUnavailable() from None
        raise NotificationDeleteUnavailable()
    try:
        return _REPLY_ADAPTER.validate_python(raw)
    except (ValidationError, ValueError, TypeError):
        raise NotificationDeleteUnavailable() from None


def delete_json_schema() -> dict[str, Any]:
    schema = _MESSAGE_ADAPTER.json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = DELETE_PROTOCOL
    schema["title"] = "AAP notification deletion protocol"
    return schema
