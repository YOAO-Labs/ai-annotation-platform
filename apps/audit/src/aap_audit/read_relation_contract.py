"""Portable row contract for Audit's fixed read-only event relation."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    field_validator,
)

from .append_contract import JSONValue

READ_RELATION_PROTOCOL = "aap-audit-read-relations/v1"
AUDIT_EVENTS_RELATION = "aap_audit_v1.audit_events"
AUDIT_EVENT_ROW_KEY = ("id", "created_at")

AUDIT_EVENT_COLUMNS = (
    "id",
    "actor_id",
    "actor_email",
    "actor_role",
    "action",
    "target_type",
    "target_id",
    "method",
    "path",
    "status_code",
    "ip",
    "detail_json",
    "request_id",
    "created_at",
)
AUDIT_EVENT_COLUMN_TYPES: dict[str, str] = {
    "id": "bigint",
    "actor_id": "uuid",
    "actor_email": "character varying(255)",
    "actor_role": "character varying(32)",
    "action": "character varying(64)",
    "target_type": "character varying(32)",
    "target_id": "character varying(64)",
    "method": "character varying(8)",
    "path": "character varying(256)",
    "status_code": "smallint",
    "ip": "inet",
    "detail_json": "jsonb",
    "request_id": "character varying(36)",
    "created_at": "timestamp with time zone",
}
AUDIT_EVENT_NULLABLE_COLUMNS = (
    "actor_id",
    "actor_email",
    "actor_role",
    "target_type",
    "target_id",
    "method",
    "path",
    "status_code",
    "ip",
    "detail_json",
    "request_id",
)

_MIN_POSTGRES_BIGINT = -(1 << 63)
_MAX_POSTGRES_BIGINT = (1 << 63) - 1
_SIGNED_BIGINT_PATTERN = r"^(?:0|-?[1-9][0-9]*)$"
_UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
    )


class AuditReadEvent(_ClosedModel):
    """Lossless JSON row representation of one `audit_events` relation row."""

    id: StrictStr = Field(
        pattern=_SIGNED_BIGINT_PATTERN,
        max_length=20,
        json_schema_extra={"format": "int64-decimal"},
    )
    actor_id: StrictStr | None = Field(
        pattern=_UUID_PATTERN,
        json_schema_extra={"format": "uuid"},
    )
    actor_email: StrictStr | None = Field(max_length=255)
    actor_role: StrictStr | None = Field(max_length=32)
    action: StrictStr = Field(max_length=64)
    target_type: StrictStr | None = Field(max_length=32)
    target_id: StrictStr | None = Field(max_length=64)
    method: StrictStr | None = Field(max_length=8)
    path: StrictStr | None = Field(max_length=256)
    status_code: StrictInt | None = Field(ge=-32768, le=32767)
    ip: StrictStr | None = Field(
        max_length=64,
        json_schema_extra={"format": "postgres-inet-text"},
    )
    detail_json: JSONValue
    request_id: StrictStr | None = Field(max_length=36)
    created_at: StrictStr = Field(json_schema_extra={"format": "date-time"})

    @field_validator("id", mode="before")
    @classmethod
    def postgres_bigint_id(cls, value: object) -> str:
        if isinstance(value, bool):
            raise ValueError("id must be a signed PostgreSQL bigint")
        if isinstance(value, int):
            value = str(value)
        if not isinstance(value, str) or not re.fullmatch(
            _SIGNED_BIGINT_PATTERN, value
        ):
            raise ValueError("id must be a canonical signed decimal string")
        parsed = int(value)
        if not _MIN_POSTGRES_BIGINT <= parsed <= _MAX_POSTGRES_BIGINT:
            raise ValueError("id exceeds PostgreSQL bigint range")
        return value

    @field_validator("actor_id", mode="before")
    @classmethod
    def canonical_actor_id(cls, value: object) -> str | None:
        if value is None:
            return None
        try:
            return str(UUID(str(value)))
        except (TypeError, ValueError):
            raise ValueError("actor_id must be a UUID") from None

    @field_validator("ip", mode="before")
    @classmethod
    def postgres_inet_text(cls, value: object) -> str | None:
        if value is None:
            return None
        rendered = str(value)
        if len(rendered) > 64:
            raise ValueError("ip exceeds the PostgreSQL INET text limit")
        return rendered

    @field_validator("created_at", mode="before")
    @classmethod
    def timestamp_text(cls, value: object) -> str:
        if isinstance(value, datetime):
            if value.utcoffset() is None:
                raise ValueError("created_at must include a timezone")
            rendered = value.isoformat()
        elif isinstance(value, str):
            rendered = value
        else:
            raise ValueError("created_at must be an ISO 8601 timestamp")
        try:
            parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("created_at must be an ISO 8601 timestamp") from None
        if parsed.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return rendered


def audit_read_relations_schema() -> dict[str, Any]:
    """Return the fixed row layout and PostgreSQL relation descriptor."""

    event_schema = TypeAdapter(AuditReadEvent).json_schema()
    definitions = dict(event_schema.pop("$defs", {}))
    definitions["AuditReadEvent"] = event_schema
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": READ_RELATION_PROTOCOL,
        "title": "Audit read-only event relations",
        "$comment": (
            "Read-only owner relation, not an authorization decision. Trusted "
            "consumers retain their existing filters, project scope, ordering, "
            "pagination and caller-owned transaction. PostgreSQL bigint IDs "
            "use canonical signed decimal strings in JSON; INET remains native "
            "in PostgreSQL and is rendered by each client boundary."
        ),
        "$defs": definitions,
        "anyOf": [{"$ref": "#/$defs/AuditReadEvent"}],
        "x-postgresql-relations": {
            AUDIT_EVENTS_RELATION: {
                "columns": list(AUDIT_EVENT_COLUMNS),
                "column_types": AUDIT_EVENT_COLUMN_TYPES,
                "nullable_columns": list(AUDIT_EVENT_NULLABLE_COLUMNS),
                "row": {"$ref": "#/$defs/AuditReadEvent"},
                "row_key": list(AUDIT_EVENT_ROW_KEY),
                "owner": "audit",
                "read_only": True,
                "security_barrier": True,
            }
        },
    }


def read_relation_json_schema() -> dict[str, Any]:
    return audit_read_relations_schema()
