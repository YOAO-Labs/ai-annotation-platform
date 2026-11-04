"""Closed JSON contract for notification preference storage operations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal, TypeAlias, Union
import uuid

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

PREFERENCES_PROTOCOL = "aap-notification-preferences/v1"
PreferenceChannel: TypeAlias = Literal["in_app", "toast"]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class PreferenceRequestBase(_ClosedModel):
    protocol: Literal["aap-notification-preferences/v1"] = Field(alias="schema")


class GetPreferencesRequest(PreferenceRequestBase):
    operation: Literal["get"]
    user_id: uuid.UUID


class PatchPreferenceRequest(PreferenceRequestBase):
    operation: Literal["patch"]
    user_id: uuid.UUID
    type: StrictStr = Field(min_length=1, max_length=60)
    patch: dict[PreferenceChannel, StrictBool]

    @field_validator("type")
    @classmethod
    def no_control_characters(cls, value: str) -> str:
        if any(ord(char) < 0x20 for char in value):
            raise ValueError("type must not contain control characters")
        return value

    @model_validator(mode="after")
    def nonempty_patch(self) -> PatchPreferenceRequest:
        if not self.patch:
            raise ValueError("patch must contain at least one channel")
        return self


class IsInAppMutedRequest(PreferenceRequestBase):
    operation: Literal["is_in_app_muted"]
    user_id: uuid.UUID
    type: StrictStr = Field(min_length=1, max_length=60)

    @field_validator("type")
    @classmethod
    def no_control_characters(cls, value: str) -> str:
        if any(ord(char) < 0x20 for char in value):
            raise ValueError("type must not contain control characters")
        return value


PreferenceRequest: TypeAlias = Annotated[
    Union[GetPreferencesRequest, PatchPreferenceRequest, IsInAppMutedRequest],
    Field(discriminator="operation"),
]


class PreferenceItem(_ClosedModel):
    type: StrictStr
    in_app: StrictBool
    email: StrictBool
    toast: StrictBool


class GetPreferencesData(_ClosedModel):
    operation: Literal["get"]
    items: list[PreferenceItem]


class PatchPreferenceData(_ClosedModel):
    operation: Literal["patch"]
    updated: Literal[True]


class IsInAppMutedData(_ClosedModel):
    operation: Literal["is_in_app_muted"]
    muted: StrictBool


class _ReplyBase(_ClosedModel):
    protocol: Literal["aap-notification-preferences/v1"] = Field(alias="schema")
    ok: Literal[True]


class GetPreferencesReply(_ReplyBase):
    data: GetPreferencesData


class PatchPreferenceReply(_ReplyBase):
    data: PatchPreferenceData


class IsInAppMutedReply(_ReplyBase):
    data: IsInAppMutedData


class PreferenceError(_ClosedModel):
    code: Literal["invalid_request", "unknown_type"]


class PreferenceFailure(_ClosedModel):
    protocol: Literal["aap-notification-preferences/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: PreferenceError


class NotificationPreferenceUnknownType(ValueError):
    def __init__(self, notification_type: str):
        self.notification_type = notification_type
        super().__init__(f"unknown notification type: {notification_type}")


PreferenceMessage: TypeAlias = Union[
    GetPreferencesRequest,
    PatchPreferenceRequest,
    IsInAppMutedRequest,
    GetPreferencesReply,
    PatchPreferenceReply,
    IsInAppMutedReply,
    PreferenceFailure,
]
_MESSAGE_ADAPTER = TypeAdapter(PreferenceMessage)


class NotificationPreferencesUnavailable(RuntimeError):
    """The PostgreSQL Notifications preference capability returned an invalid reply."""


def _reply_object(value: Any) -> Mapping[str, Any]:
    try:
        if isinstance(value, (str, bytes, bytearray)):
            value = json.loads(value)
        if not isinstance(value, Mapping):
            raise ValueError("reply must be an object")
        return value
    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError):
        raise NotificationPreferencesUnavailable() from None


def _decode(value: Any, model: type[_ClosedModel]):
    try:
        return model.model_validate(_reply_object(value))
    except (ValidationError, ValueError, TypeError):
        raise NotificationPreferencesUnavailable() from None


def decode_get_preferences_reply(value: Any) -> GetPreferencesReply:
    return _decode(value, GetPreferencesReply)


def decode_patch_preference_reply(
    value: Any, notification_type: str
) -> PatchPreferenceReply:
    raw = _reply_object(value)
    if raw.get("ok") is False:
        try:
            failure = PreferenceFailure.model_validate(raw)
        except (ValidationError, ValueError, TypeError):
            raise NotificationPreferencesUnavailable() from None
        if failure.error.code == "unknown_type":
            raise NotificationPreferenceUnknownType(notification_type)
        raise NotificationPreferencesUnavailable()
    return _decode(raw, PatchPreferenceReply)


def decode_is_in_app_muted_reply(value: Any) -> IsInAppMutedReply:
    return _decode(value, IsInAppMutedReply)


def preferences_json_schema() -> dict[str, Any]:
    schema = _MESSAGE_ADAPTER.json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = PREFERENCES_PROTOCOL
    schema["title"] = "AAP notification preferences protocol"
    return schema
