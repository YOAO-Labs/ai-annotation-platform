"""Portable command and receipt contract for transactional project deletion."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    TypeAdapter,
    model_validator,
)

from aap_audit_contract import ProjectDeletionAuditContext
from aap_workspace_contract.wire import _json_bytes

PROJECT_DELETION_PROTOCOL = "aap-project-delete/v1"
NATIVE_SCHEMA = "aap_workspace_v1"


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectDeletionCommand(_Value):
    """The frozen four-field command accepted by the existing native function."""

    operation_id: UUID
    actor_id: UUID
    project_id: UUID
    # No default: null is allowed, but the field is required on the wire.
    audit_context: ProjectDeletionAuditContext | None


class ProjectDeletionFailure(Exception):
    """A stable business failure returned by the native deletion function."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ProjectDeletionReceipt(_Value):
    """The public immutable receipt; keep this shape aligned with frozen v1."""

    operation_id: UUID
    actor_id: UUID
    project_id: UUID
    deleted_tasks: Annotated[int, Field(strict=True, ge=0)]
    deleted_annotations: Annotated[int, Field(strict=True, ge=0)]
    audit_id: Annotated[int, Field(strict=True, gt=0)]


class _Failure(_Value):
    # Codes remain open so a newer native function can report a new stable code.
    code: str


class _Reply(_Value):
    schema_version: Literal["aap-project-delete/v1"] = Field(alias="schema")
    ok: StrictBool
    data: ProjectDeletionReceipt | None = None
    error: _Failure | None = None

    @model_validator(mode="after")
    def check_branch(self):
        if self.ok:
            if self.data is None or "error" in self.model_fields_set:
                raise ValueError("Successful deletion must have only its receipt")
        elif self.error is None or "data" in self.model_fields_set:
            raise ValueError("Failed deletion must have only its stable error")
        return self


class _Success(_Value):
    schema_version: Literal["aap-project-delete/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: ProjectDeletionReceipt


class _Error(_Value):
    code: str


class _FailureReply(_Value):
    schema_version: Literal["aap-project-delete/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: _Error


def _payload_value(payload: object) -> object:
    if isinstance(payload, (bytes, str)):
        return json.loads(_json_bytes(payload))
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json", by_alias=True)
    if isinstance(payload, Mapping):
        return dict(payload)
    return payload


def decode_project_deletion_command(
    payload: ProjectDeletionCommand | Mapping[str, object] | bytes | str,
) -> ProjectDeletionCommand:
    """Validate a command while preserving UUID-string coercion and closed fields."""
    return ProjectDeletionCommand.model_validate(_payload_value(payload))


def decode_project_deletion_reply(
    payload: object,
) -> ProjectDeletionReceipt:
    """Decode the frozen reply, rejecting ambiguous/non-finite raw JSON."""
    reply = _Reply.model_validate(_payload_value(payload))
    if not reply.ok:
        assert reply.error is not None
        raise ProjectDeletionFailure(reply.error.code)
    assert reply.data is not None
    return reply.data


def project_deletion_schema() -> dict[str, Any]:
    """Return the current portable command/reply descriptor for artifact generation."""
    schema = TypeAdapter(
        ProjectDeletionCommand | _Success | _FailureReply
    ).json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = PROJECT_DELETION_PROTOCOL
    schema["title"] = "Transactional Workspace project deletion v1"
    schema["oneOf"] = schema.pop("anyOf")
    signature = f"{NATIVE_SCHEMA}.delete_project(uuid, uuid, uuid, jsonb)"
    schema["x-native-capabilities"] = {
        "protocol": PROJECT_DELETION_PROTOCOL,
        "postgres_schema": NATIVE_SCHEMA,
        "functions": [
            {
                "name": "delete_project",
                "signature": signature,
                "arguments": [
                    {"name": "operation_id", "postgres_type": "uuid"},
                    {"name": "actor_id", "postgres_type": "uuid"},
                    {"name": "project_id", "postgres_type": "uuid"},
                    {
                        "name": "audit_context",
                        "postgres_type": "jsonb",
                        "nullable": True,
                    },
                ],
                "result_postgres_type": "jsonb",
                "transaction_semantics": (
                    "The binding borrows the caller's already-active transaction. "
                    "Credential admission and commit remain caller-owned; the "
                    "Native coordinator rechecks current authority. A failed outer "
                    "commit rolls back the project graph, audit row, and receipt."
                ),
            }
        ],
        "authorization_boundary": (
            "The application caller retains credential admission. The Native "
            "coordinator rechecks the active account and current project manager "
            "before first deletion; receipt replay retains its existing active-actor rule."
        ),
        "transaction_boundary": "The caller owns the active transaction and commit.",
    }
    return schema


__all__ = [
    "NATIVE_SCHEMA",
    "PROJECT_DELETION_PROTOCOL",
    "ProjectDeletionCommand",
    "ProjectDeletionFailure",
    "ProjectDeletionReceipt",
    "decode_project_deletion_command",
    "decode_project_deletion_reply",
    "project_deletion_schema",
]
