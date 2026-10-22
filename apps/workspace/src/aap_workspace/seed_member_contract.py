"""Closed contract for trusted seed ProjectMember writes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import uuid
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    Field,
    StrictBool,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from aap_workspace_contract.wire import ClosedModel, _json_bytes


SEED_MEMBER_PROTOCOL = "aap-workspace-seed-members/v1"
NATIVE_SCHEMA = "aap_workspace_v1"


class _SeedMemberModel(ClosedModel):
    @model_validator(mode="before")
    @classmethod
    def reject_protocol_alias(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class SeedMemberInsert(_SeedMemberModel):
    """One exact insert from the ordered trusted seed operation list."""

    kind: Literal["insert"]
    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    role: StrictStr
    assigned_by: uuid.UUID


class SeedMemberUpdate(_SeedMemberModel):
    """One mapper-mask update; flags preserve whether each column was dirty."""

    kind: Literal["update"]
    id: uuid.UUID
    role: StrictStr
    assigned_by: uuid.UUID
    write_role: StrictBool
    write_assigned_by: StrictBool

    @model_validator(mode="after")
    def has_write_column(self) -> SeedMemberUpdate:
        if not self.write_role and not self.write_assigned_by:
            raise ValueError("Seed ProjectMember update has no changed column")
        return self


SeedMemberWriteOperation: TypeAlias = Annotated[
    SeedMemberInsert | SeedMemberUpdate,
    Field(discriminator="kind"),
]


class SeedMemberWriteRequest(_SeedMemberModel):
    """Ordered caller-owned ProjectMember insert/update operations."""

    protocol: Literal["aap-workspace-seed-members/v1"] = Field(alias="schema")
    operations: list[SeedMemberWriteOperation]


class SeedMemberInsertResult(_SeedMemberModel):
    kind: Literal["insert"]
    id: uuid.UUID
    assigned_at: datetime = Field(
        json_schema_extra={"x-timezone-aware": True, "x-source-timezone": "UTC"}
    )
    updated_at: datetime = Field(
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
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Seed ProjectMember timestamp must be timezone-aware")
        return value


class SeedMemberUpdateResult(_SeedMemberModel):
    kind: Literal["update"]
    id: uuid.UUID
    found: StrictBool
    updated_at: datetime | None = Field(
        json_schema_extra={"x-timezone-aware": True, "x-source-timezone": "UTC"}
    )

    @field_validator("updated_at", mode="before")
    @classmethod
    def parse_timestamp_text(cls, value):
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("Invalid seed ProjectMember timestamp") from None
        return value

    @field_validator("updated_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("Seed ProjectMember timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def timestamp_matches_found(self) -> SeedMemberUpdateResult:
        if self.found != (self.updated_at is not None):
            raise ValueError("Seed ProjectMember update result has inconsistent scope")
        return self


SeedMemberWriteResult: TypeAlias = Annotated[
    SeedMemberInsertResult | SeedMemberUpdateResult,
    Field(discriminator="kind"),
]


class SeedMemberWriteReply(_SeedMemberModel):
    protocol: Literal["aap-workspace-seed-members/v1"] = Field(alias="schema")
    results: list[SeedMemberWriteResult]


_REQUEST_ADAPTER = TypeAdapter(SeedMemberWriteRequest)
_REPLY_ADAPTER = TypeAdapter(SeedMemberWriteReply)


class SeedMemberWriteUnavailable(RuntimeError):
    """The native Workspace seed-members capability returned invalid data."""

    def __init__(self):
        super().__init__("Workspace seed ProjectMember write unavailable")


def decode_seed_member_write_request(
    payload: SeedMemberWriteRequest | dict | bytes | str,
) -> SeedMemberWriteRequest:
    """Validate an exact ordered request before sending it to PostgreSQL."""
    try:
        return _REQUEST_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise SeedMemberWriteUnavailable() from None


def decode_seed_member_write_reply(
    payload: dict | bytes | str | SeedMemberWriteReply,
    request: SeedMemberWriteRequest,
) -> list[SeedMemberWriteResult]:
    """Validate result shape and bind each result to its ordered operation."""
    if not isinstance(request, SeedMemberWriteRequest):
        raise SeedMemberWriteUnavailable()
    try:
        reply = _REPLY_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise SeedMemberWriteUnavailable() from None

    if len(reply.results) != len(request.operations):
        raise SeedMemberWriteUnavailable()
    for operation, result in zip(request.operations, reply.results, strict=True):
        if operation.kind != result.kind or operation.id != result.id:
            raise SeedMemberWriteUnavailable()
    return reply.results


@dataclass(frozen=True)
class _NativeFunction:
    name: str
    arguments: tuple[tuple[str, str, TypeAdapter[Any]], ...]
    result_model: type[ClosedModel]
    transaction_semantics: str


_REQUEST_SCHEMA = TypeAdapter(SeedMemberWriteRequest)
_NATIVE_FUNCTIONS = (
    _NativeFunction(
        name="write_seed_members",
        arguments=(("p_request", "jsonb", _REQUEST_SCHEMA),),
        result_model=SeedMemberWriteReply,
        transaction_semantics=(
            "Apply the supplied ProjectMember insert/update operations in array order "
            "inside the caller's transaction. Inserts use the existing ProjectMember "
            "defaults; updates write only their explicit changed-column mask and stamp "
            "updated_at with now() without incrementing version. A missing update target "
            "returns found=false with a null updated_at for the caller to map to its "
            "legacy stale-row error. This fixed capability adds no authorization, audit, "
            "commit, retry, upsert, role policy, or generic unit-of-work behavior. "
            "Uniqueness, foreign-key, and length errors remain database errors."
        ),
    ),
)


def seed_member_schema() -> dict[str, Any]:
    """Return reply schemas and the exact fixed seed-write capability descriptor."""
    schema = _REPLY_ADAPTER.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = SEED_MEMBER_PROTOCOL
    schema.setdefault("$defs", {})["SeedMemberWriteReply"] = {
        key: value
        for key, value in schema.items()
        if key not in {"$defs", "$schema", "$id", "x-native-capabilities"}
    }
    schema["x-native-capabilities"] = {
        "protocol": SEED_MEMBER_PROTOCOL,
        "postgres_schema": NATIVE_SCHEMA,
        "functions": [
            {
                "name": function.name,
                "signature": (
                    f"{NATIVE_SCHEMA}.{function.name}"
                    f"({', '.join(argument[1] for argument in function.arguments)})"
                ),
                "arguments": [
                    {
                        "name": name,
                        "postgres_type": postgres_type,
                        "json_schema": adapter.json_schema(),
                    }
                    for name, postgres_type, adapter in function.arguments
                ],
                "result_postgres_type": "jsonb",
                "result_model": function.result_model.__name__,
                "result_schema_ref": f"#/$defs/{function.result_model.__name__}",
                "transaction_semantics": function.transaction_semantics,
            }
            for function in _NATIVE_FUNCTIONS
        ],
        "authorization_boundary": (
            "The trusted seed caller retains authorization and chooses the ordered "
            "operations; this fixed capability adds no application authorization."
        ),
        "transaction_boundary": "The caller owns the active transaction and commit.",
    }
    return schema


__all__ = [
    "NATIVE_SCHEMA",
    "SEED_MEMBER_PROTOCOL",
    "SeedMemberInsert",
    "SeedMemberInsertResult",
    "SeedMemberUpdate",
    "SeedMemberUpdateResult",
    "SeedMemberWriteOperation",
    "SeedMemberWriteReply",
    "SeedMemberWriteRequest",
    "SeedMemberWriteResult",
    "SeedMemberWriteUnavailable",
    "decode_seed_member_write_reply",
    "decode_seed_member_write_request",
    "seed_member_schema",
]
