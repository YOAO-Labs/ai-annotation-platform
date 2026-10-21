"""Closed wire contract for the single Workspace project-owner transfer."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from aap_workspace_contract.wire import ClosedModel, _json_bytes

OWNER_TRANSFER_PROTOCOL = "aap-workspace-owner-transfer/v1"
OwnerTransferStatusCode: TypeAlias = Annotated[StrictInt, Field(ge=400, le=599)]


class _OwnerTransferModel(ClosedModel):
    @model_validator(mode="before")
    @classmethod
    def declared_schema_field_only(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class OwnerTransferRequest(_OwnerTransferModel):
    protocol: Literal["aap-workspace-owner-transfer/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    actor_id: uuid.UUID
    project_id: uuid.UUID
    new_owner_id: uuid.UUID


class OwnerTransferPolicy(_OwnerTransferModel):
    protocol: Literal["aap-workspace-owner-transfer/v1"] = Field(alias="schema")


class OwnerTransferResult(ClosedModel):
    previous_owner_id: uuid.UUID | None
    updated_at: datetime
    changed: StrictBool

    @field_validator("updated_at", mode="before")
    @classmethod
    def parse_timestamp_text(cls, value):
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("Invalid owner-transfer timestamp") from None
        return value

    @field_validator("updated_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Owner-transfer timestamp must be timezone-aware")
        return value


class OwnerTransferSuccess(_OwnerTransferModel):
    protocol: Literal["aap-workspace-owner-transfer/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    actor_id: uuid.UUID
    project_id: uuid.UUID
    new_owner_id: uuid.UUID
    data: OwnerTransferResult

    @field_validator("ok", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value


class OwnerTransferError(ClosedModel):
    status_code: OwnerTransferStatusCode
    detail: JsonValue
    rollback: StrictBool


class OwnerTransferFailure(_OwnerTransferModel):
    protocol: Literal["aap-workspace-owner-transfer/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: OwnerTransferError

    @field_validator("ok", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value


OwnerTransferReply: TypeAlias = OwnerTransferSuccess | OwnerTransferFailure


class OwnerTransferUnavailable(Exception):
    """The native Workspace owner-transfer capability is unavailable."""

    def __init__(self):
        super().__init__("Workspace owner transfer unavailable")


class OwnerTransferInvalidRequest(ValueError):
    """A malformed request does not match the frozen owner-transfer protocol."""

    def __init__(self):
        super().__init__("Invalid Workspace owner-transfer request")


_REQUEST_ADAPTER = TypeAdapter(OwnerTransferRequest)
_POLICY_ADAPTER = TypeAdapter(OwnerTransferPolicy)
_REPLY_ADAPTER = TypeAdapter(OwnerTransferReply)


def _validated_request(request: OwnerTransferRequest) -> OwnerTransferRequest:
    if not isinstance(request, OwnerTransferRequest):
        raise OwnerTransferUnavailable()
    try:
        return _REQUEST_ADAPTER.validate_json(_json_bytes(request))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise OwnerTransferUnavailable() from None


def decode_owner_transfer_request(
    payload: dict | bytes | str | OwnerTransferRequest,
) -> OwnerTransferRequest:
    try:
        return _REQUEST_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise OwnerTransferInvalidRequest() from None


def decode_owner_transfer_policy(
    payload: dict | bytes | str,
) -> OwnerTransferPolicy:
    try:
        return _POLICY_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise OwnerTransferUnavailable() from None


def decode_owner_transfer_reply(
    request: OwnerTransferRequest,
    payload: dict | bytes | str | OwnerTransferReply,
) -> OwnerTransferReply:
    request = _validated_request(request)
    try:
        encoded = _json_bytes(payload)
        raw = _REPLY_ADAPTER.validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise OwnerTransferUnavailable() from None

    if isinstance(raw, OwnerTransferSuccess):
        if (
            raw.operation_id != request.operation_id
            or raw.actor_id != request.actor_id
            or raw.project_id != request.project_id
            or raw.new_owner_id != request.new_owner_id
            or raw.data.changed != (raw.data.previous_owner_id != request.new_owner_id)
        ):
            raise OwnerTransferUnavailable()
    return raw


def owner_transfer_failure_reply(error: OwnerTransferError) -> OwnerTransferFailure:
    return OwnerTransferFailure(
        schema=OWNER_TRANSFER_PROTOCOL,
        ok=False,
        error=error,
    )
