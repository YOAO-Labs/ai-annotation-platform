"""Closed contract for pinning a trusted seed ProjectMember assignment time."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import uuid
from typing import Any, Literal

from pydantic import (
    Field,
    StrictBool,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from aap_workspace_contract.wire import ClosedModel, _json_bytes

SEED_MEMBER_ASSIGNMENT_PROTOCOL = "aap-workspace-seed-member-assignment/v1"
NATIVE_SCHEMA = "aap_workspace_v1"


class _SeedMemberAssignmentModel(ClosedModel):
    @model_validator(mode="before")
    @classmethod
    def reject_protocol_alias(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class SeedMemberAssignmentReceipt(_SeedMemberAssignmentModel):
    """Receipt for pinning one existing ProjectMember's assigned_at value."""

    protocol: Literal["aap-workspace-seed-member-assignment/v1"] = Field(alias="schema")
    member_id: uuid.UUID
    found: StrictBool
    assigned_at: datetime | None = Field(
        json_schema_extra={"x-timezone-aware": True, "x-source-timezone": "UTC"}
    )
    updated_at: datetime | None = Field(
        json_schema_extra={"x-timezone-aware": True, "x-source-timezone": "UTC"}
    )

    @field_validator("assigned_at", "updated_at", mode="before")
    @classmethod
    def parse_timestamp_text(cls, value):
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("Invalid seed ProjectMember timestamp") from None
        return value

    @field_validator("assigned_at", "updated_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("Seed ProjectMember timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def timestamps_match_found(self) -> SeedMemberAssignmentReceipt:
        if self.found and (self.assigned_at is None or self.updated_at is None):
            raise ValueError("Found seed ProjectMember must have both timestamps")
        if not self.found and (
            self.assigned_at is not None or self.updated_at is not None
        ):
            raise ValueError("Missing seed ProjectMember must have null timestamps")
        return self


_RECEIPT_ADAPTER = TypeAdapter(SeedMemberAssignmentReceipt)
_UUID_ADAPTER = TypeAdapter(uuid.UUID)
_DATETIME_ADAPTER = TypeAdapter(datetime)


class SeedMemberAssignmentUnavailable(RuntimeError):
    """The native Workspace seed assignment capability returned invalid data."""

    def __init__(self):
        super().__init__("Workspace seed ProjectMember assignment unavailable")


def decode_seed_member_assignment_reply(
    payload: dict | bytes | str | SeedMemberAssignmentReceipt,
    *,
    member_id: uuid.UUID,
    assigned_at: datetime,
) -> SeedMemberAssignmentReceipt:
    """Validate a receipt and bind it to the requested member and timestamp."""
    if not isinstance(member_id, uuid.UUID) or not isinstance(assigned_at, datetime):
        raise SeedMemberAssignmentUnavailable()
    if assigned_at.utcoffset() is None:
        raise SeedMemberAssignmentUnavailable()

    try:
        reply = _RECEIPT_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise SeedMemberAssignmentUnavailable() from None

    if reply.member_id != member_id or (
        reply.found and reply.assigned_at != assigned_at
    ):
        raise SeedMemberAssignmentUnavailable()
    return reply


@dataclass(frozen=True)
class _NativeArgument:
    name: str
    postgres_type: str
    json_schema: dict[str, Any]


@dataclass(frozen=True)
class _NativeFunction:
    name: str
    arguments: tuple[_NativeArgument, ...]
    transaction_semantics: str


_timestamp_schema = _DATETIME_ADAPTER.json_schema()
_timestamp_schema.update({"x-timezone-aware": True, "x-source-timezone": "UTC"})
_NATIVE_FUNCTIONS = (
    _NativeFunction(
        name="pin_seed_member_assignment",
        arguments=(
            _NativeArgument("p_member_id", "uuid", _UUID_ADAPTER.json_schema()),
            _NativeArgument("p_assigned_at", "timestamptz", _timestamp_schema),
        ),
        transaction_semantics=(
            "For the exact existing ProjectMember, always set assigned_at to the "
            "requested value and updated_at=now() using normal UPDATE row-lock "
            "waiting. The caller invokes this function only when its original "
            "ORM history marked assigned_at dirty, so a stale cached value still "
            "preserves the original write even if the stored value has since "
            "become equal. A missing row returns found=false with both timestamps "
            "null. NULL arguments raise 22023. No upsert, authorization, "
            "role/version/weekly_target changes, retry, or independent transaction "
            "is added; the caller owns commit or rollback and ordinary database "
            "errors propagate."
        ),
    ),
)


def seed_member_assignment_schema() -> dict[str, Any]:
    """Return the receipt schema and exact fixed native capability descriptor."""
    schema = _RECEIPT_ADAPTER.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = SEED_MEMBER_ASSIGNMENT_PROTOCOL
    schema.setdefault("$defs", {})["SeedMemberAssignmentReceipt"] = {
        key: value
        for key, value in schema.items()
        if key not in {"$defs", "$schema", "$id", "x-native-capabilities"}
    }
    schema["x-native-capabilities"] = {
        "protocol": SEED_MEMBER_ASSIGNMENT_PROTOCOL,
        "postgres_schema": NATIVE_SCHEMA,
        "functions": [
            {
                "name": function.name,
                "signature": (
                    f"{NATIVE_SCHEMA}.{function.name}"
                    f"({', '.join(argument.postgres_type for argument in function.arguments)})"
                ),
                "arguments": [
                    {
                        "name": argument.name,
                        "postgres_type": argument.postgres_type,
                        "json_schema": argument.json_schema,
                    }
                    for argument in function.arguments
                ],
                "result_postgres_type": "jsonb",
                "result_model": "SeedMemberAssignmentReceipt",
                "result_schema_ref": "#/$defs/SeedMemberAssignmentReceipt",
                "volatility": "VOLATILE",
                "transaction_semantics": function.transaction_semantics,
            }
            for function in _NATIVE_FUNCTIONS
        ],
        "authorization_boundary": (
            "The trusted development seed caller retains authorization; this fixed "
            "capability adds no application authorization."
        ),
        "transaction_boundary": "The caller owns the active transaction and commit or rollback.",
    }
    return schema


__all__ = [
    "NATIVE_SCHEMA",
    "SEED_MEMBER_ASSIGNMENT_PROTOCOL",
    "SeedMemberAssignmentReceipt",
    "SeedMemberAssignmentUnavailable",
    "decode_seed_member_assignment_reply",
    "seed_member_assignment_schema",
]
