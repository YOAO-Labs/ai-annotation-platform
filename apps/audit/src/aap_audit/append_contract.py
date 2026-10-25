"""Closed wire contract for immutable audit append commands and receipts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, Union
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from typing_extensions import TypeAliasType

APPEND_PROTOCOL = "aap-audit-append/v1"
_MAX_POSTGRES_BIGINT = (1 << 63) - 1
_POSTGRES_BIGINT_ID_PATTERN = (
    r"^(?:"
    r"[1-9][0-9]{0,17}|[1-8][0-9]{18}|9[01][0-9]{17}|92[01][0-9]{16}|"
    r"922[0-2][0-9]{15}|9223[0-2][0-9]{14}|92233[0-6][0-9]{13}|"
    r"922337[01][0-9]{12}|92233720[0-2][0-9]{10}|922337203[0-5][0-9]{9}|"
    r"9223372036[0-7][0-9]{8}|92233720368[0-4][0-9]{7}|"
    r"922337203685[0-3][0-9]{6}|9223372036854[0-6][0-9]{5}|"
    r"92233720368547[0-6][0-9]{4}|922337203685477[0-4][0-9]{3}|"
    r"9223372036854775[0-7][0-9]{2}|922337203685477580[0-7]"
    r")$"
)
JSONValue = TypeAliasType(
    "JSONValue",
    Union[
        dict[str, "JSONValue"],
        list["JSONValue"],
        StrictStr,
        StrictInt,
        StrictFloat,
        StrictBool,
        None,
    ],
)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False
    )


class AppendEvent(_ClosedModel):
    actor_id: UUID | None
    actor_email: StrictStr | None = Field(max_length=255)
    actor_role: StrictStr | None = Field(max_length=32)
    action: StrictStr = Field(max_length=64)
    target_type: StrictStr | None = Field(max_length=32)
    target_id: StrictStr | None = Field(max_length=64)
    method: StrictStr | None = Field(max_length=8)
    path: StrictStr | None = Field(max_length=256)
    status_code: StrictInt | None = Field(ge=-32768, le=32767)
    ip: StrictStr | None = Field(max_length=64)
    detail_json: JSONValue
    request_id: StrictStr | None = Field(max_length=36)


class AppendRequest(_ClosedModel):
    protocol: Literal["aap-audit-append/v1"] = Field(alias="schema")
    events: list[AppendEvent] = Field(min_length=1)

    @classmethod
    def create(cls, events: list[AppendEvent]) -> AppendRequest:
        return cls(schema=APPEND_PROTOCOL, events=events)


class AppendReceipt(_ClosedModel):
    id: StrictStr = Field(pattern=_POSTGRES_BIGINT_ID_PATTERN, max_length=19)
    created_at: StrictStr = Field(json_schema_extra={"format": "date-time"})

    @field_validator("id")
    @classmethod
    def postgres_bigint_id(cls, value: str) -> str:
        if int(value) > _MAX_POSTGRES_BIGINT:
            raise ValueError("id exceeds PostgreSQL bigint range")
        return value

    @field_validator("created_at")
    @classmethod
    def timezone_required(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("created_at must be an ISO 8601 timestamp") from None
        if parsed.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        return value


class AppendData(_ClosedModel):
    events: list[AppendReceipt]


class AppendReply(_ClosedModel):
    protocol: Literal["aap-audit-append/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: AppendData


class AuditAppendUnavailable(RuntimeError):
    """The PostgreSQL Audit owner returned a malformed or foreign reply."""


def decode_append_reply(value: Any) -> AppendReply:
    try:
        if isinstance(value, (str, bytes, bytearray)):
            raw = json.loads(value)
        elif isinstance(value, Mapping):
            raw = dict(value)
        else:
            raise ValueError("reply must be a JSON object")
        return AppendReply.model_validate(raw)
    except (json.JSONDecodeError, UnicodeError, ValidationError, ValueError, TypeError):
        raise AuditAppendUnavailable() from None


def append_json_schema() -> dict[str, Any]:
    schema = _MESSAGE_ADAPTER.json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = APPEND_PROTOCOL
    schema["title"] = "AAP audit append protocol"
    return schema


AppendMessage = AppendRequest | AppendReply
_MESSAGE_ADAPTER = TypeAdapter(AppendMessage)
