"""Closed wire contract for global Audit list and export reads."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, TypeAlias
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from .append_contract import JSONValue

QUERY_PROTOCOL = "aap-audit-query/v1"
EXPORT_MAX_ROWS = 50_000
_MIN_POSTGRES_BIGINT = -(1 << 63)
_MAX_POSTGRES_BIGINT = (1 << 63) - 1
_COUNT_PATTERN = r"^(?:0|[1-9][0-9]{0,18})$"
_SIGNED_BIGINT_PATTERN = r"^(?:0|-?[1-9][0-9]*)$"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
        populate_by_name=True,
    )


class AuditQueryFilters(_ClosedModel):
    action: StrictStr | None
    target_type: StrictStr | None
    target_id: StrictStr | None
    actor_id: StrictStr | None
    from_: datetime | None = Field(alias="from")
    to: datetime | None
    business_only: StrictBool
    detail_key: StrictStr | None
    detail_value: StrictStr | None

    @field_validator("from_", "to")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("query time bounds must include a timezone")
        return value


class AuditQueryAfter(_ClosedModel):
    created_at: StrictStr = Field(json_schema_extra={"format": "date-time"})
    id: StrictStr = Field(pattern=r"^-?[0-9]+$", max_length=20)

    @field_validator("created_at")
    @classmethod
    def timestamp_string(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("created_at must be an ISO 8601 timestamp") from None
        if parsed.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value

    @field_validator("id")
    @classmethod
    def postgres_bigint_cursor(cls, value: str) -> str:
        parsed = int(value)
        if not _MIN_POSTGRES_BIGINT <= parsed <= _MAX_POSTGRES_BIGINT:
            raise ValueError("cursor id exceeds PostgreSQL bigint range")
        return value


class AuditQueryRequest(_ClosedModel):
    protocol: Literal["aap-audit-query/v1"] = Field(alias="schema")
    operation: Literal["list", "export"]
    filters: AuditQueryFilters
    page: StrictInt = Field(ge=1)
    page_size: StrictInt = Field(ge=1, le=EXPORT_MAX_ROWS)
    after: AuditQueryAfter | None

    @model_validator(mode="after")
    def operation_bounds(self) -> AuditQueryRequest:
        if self.operation == "list" and self.page_size > 200:
            raise ValueError("list page_size exceeds 200")
        if self.operation == "export" and (
            self.page != 1
            or self.page_size != EXPORT_MAX_ROWS
            or self.after is not None
        ):
            raise ValueError("export uses the fixed row cap without pagination")
        return self

    @classmethod
    def list_page(
        cls,
        filters: AuditQueryFilters,
        *,
        page: int,
        page_size: int,
        after: AuditQueryAfter | None = None,
    ) -> AuditQueryRequest:
        return cls(
            schema=QUERY_PROTOCOL,
            operation="list",
            filters=filters,
            page=page,
            page_size=page_size,
            after=after,
        )

    @classmethod
    def export(cls, filters: AuditQueryFilters) -> AuditQueryRequest:
        return cls(
            schema=QUERY_PROTOCOL,
            operation="export",
            filters=filters,
            page=1,
            page_size=EXPORT_MAX_ROWS,
            after=None,
        )


class AuditQueryEvent(_ClosedModel):
    id: StrictStr = Field(pattern=_SIGNED_BIGINT_PATTERN, max_length=20)
    actor_id: StrictStr | None = Field(json_schema_extra={"format": "uuid"})
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
    created_at: StrictStr = Field(json_schema_extra={"format": "date-time"})

    @field_validator("id")
    @classmethod
    def postgres_bigint_id(cls, value: str) -> str:
        if not _MIN_POSTGRES_BIGINT <= int(value) <= _MAX_POSTGRES_BIGINT:
            raise ValueError("id exceeds PostgreSQL bigint range")
        return value

    @field_validator("actor_id")
    @classmethod
    def actor_uuid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return str(UUID(value))

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


class AuditQueryData(_ClosedModel):
    operation: Literal["list", "export"]
    total: StrictStr = Field(pattern=_COUNT_PATTERN, max_length=19)
    events: list[AuditQueryEvent]

    @field_validator("total")
    @classmethod
    def postgres_bigint_total(cls, value: str) -> str:
        if int(value) > _MAX_POSTGRES_BIGINT:
            raise ValueError("total exceeds PostgreSQL bigint range")
        return value


class AuditQueryReply(_ClosedModel):
    protocol: Literal["aap-audit-query/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: AuditQueryData


class AuditQueryUnavailable(RuntimeError):
    """The PostgreSQL Audit query owner returned a malformed or foreign reply."""


def decode_query_reply(value: Any) -> AuditQueryReply:
    try:
        if isinstance(value, (str, bytes, bytearray)):
            raw = json.loads(value)
        elif isinstance(value, Mapping):
            raw = dict(value)
        else:
            raise ValueError("reply must be a JSON object")
        return AuditQueryReply.model_validate(raw)
    except (json.JSONDecodeError, UnicodeError, ValidationError, ValueError, TypeError):
        raise AuditQueryUnavailable() from None


def validate_query_reply(
    request: AuditQueryRequest, reply: AuditQueryReply
) -> AuditQueryReply:
    if reply.data.operation != request.operation:
        raise AuditQueryUnavailable()
    total = int(reply.data.total)
    events = reply.data.events
    if request.operation == "list":
        if len(events) > min(request.page_size, total):
            raise AuditQueryUnavailable()
    elif total > EXPORT_MAX_ROWS:
        if events:
            raise AuditQueryUnavailable()
    elif len(events) != total:
        raise AuditQueryUnavailable()
    keys = {
        (event.id, datetime.fromisoformat(event.created_at.replace("Z", "+00:00")))
        for event in events
    }
    if len(keys) != len(events):
        raise AuditQueryUnavailable()
    return reply


AuditQueryMessage: TypeAlias = AuditQueryRequest | AuditQueryReply
_MESSAGE_ADAPTER = TypeAdapter(AuditQueryMessage)


def query_json_schema() -> dict[str, Any]:
    schema = _MESSAGE_ADAPTER.json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = QUERY_PROTOCOL
    schema["title"] = "AAP audit query protocol"
    return schema
