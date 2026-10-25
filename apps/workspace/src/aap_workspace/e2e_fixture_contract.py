"""Closed contracts for guarded Workspace E2E fixture operations."""

from __future__ import annotations

from typing import Any, Literal
import uuid

from pydantic import (
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from aap_workspace_contract.wire import ClosedModel as _ClosedModel, _json_bytes

PROTOCOL = "aap-workspace-e2e-fixtures/v1"
NATIVE_SCHEMA = "aap_workspace_fixture_v1"


class _FixtureModel(_ClosedModel):
    @model_validator(mode="before")
    @classmethod
    def use_declared_schema_field(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class E2EProjectRecord(_FixtureModel):
    """Exact input fields passed to the fixed E2E Project creator."""

    id: uuid.UUID
    display_id: StrictStr
    name: StrictStr
    type_label: StrictStr
    type_key: StrictStr
    data_type: StrictStr
    owner_id: uuid.UUID
    tool_bindings: dict[StrictStr, JsonValue]
    scene_mode: StrictBool
    ai_enabled: StrictBool


class E2ETemplateRecord(_FixtureModel):
    """Exact input fields passed to the fixed E2E Template creator."""

    id: uuid.UUID
    display_id: StrictStr
    name: StrictStr
    description: StrictStr | None
    type_label: StrictStr
    type_key: StrictStr
    data_type: StrictStr
    tool_bindings: dict[StrictStr, JsonValue]
    label_config: dict[StrictStr, JsonValue]
    scope: StrictStr
    created_by: uuid.UUID
    source_project_id: uuid.UUID | None


class E2EProjectCleanupGuard(_FixtureModel):
    """Closed result of the Workspace video-retirement preflight."""

    protocol: Literal["aap-workspace-e2e-fixtures/v1"] = Field(alias="schema")
    ok: StrictBool
    reason: Literal["video_execution_not_retired"] | None

    @model_validator(mode="after")
    def reason_matches_result(self) -> E2EProjectCleanupGuard:
        if self.ok != (self.reason is None):
            raise ValueError("Workspace cleanup guard result/reason mismatch")
        return self


class WorkspaceE2EFixtureUnavailable(RuntimeError):
    """A fixed Workspace E2E fixture capability is unavailable or malformed."""

    def __init__(self):
        super().__init__("Workspace E2E fixture capability unavailable")


_COUNT_ADAPTER = TypeAdapter(StrictInt)
_CLEANUP_GUARD_ADAPTER = TypeAdapter(E2EProjectCleanupGuard)
_PROJECT_ID_ADAPTER = TypeAdapter(uuid.UUID)
_REPLY_ADAPTER = TypeAdapter(
    E2EProjectRecord | E2ETemplateRecord | E2EProjectCleanupGuard
)


def decode_fixture_project_id(payload: Any, expected: uuid.UUID) -> uuid.UUID:
    """Validate a scalar UUID receipt and bind it to the requested Project."""
    if not isinstance(expected, uuid.UUID):
        raise WorkspaceE2EFixtureUnavailable()
    try:
        if isinstance(payload, uuid.UUID):
            project_id = payload
        elif type(payload) is str:
            project_id = _PROJECT_ID_ADAPTER.validate_python(payload)
            if str(project_id) != payload:
                raise ValueError("non-canonical UUID")
        else:
            raise ValueError("invalid UUID result")
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise WorkspaceE2EFixtureUnavailable() from None
    if project_id != expected:
        raise WorkspaceE2EFixtureUnavailable()
    return project_id


def decode_fixture_count(payload: Any, expected_max: int | None = None) -> int:
    """Validate a nonnegative row count and optional caller-known upper bound."""
    if expected_max is not None and (type(expected_max) is not int or expected_max < 0):
        raise WorkspaceE2EFixtureUnavailable()
    try:
        count = _COUNT_ADAPTER.validate_python(payload)
    except (ValidationError, ValueError, TypeError):
        raise WorkspaceE2EFixtureUnavailable() from None
    if count < 0 or (expected_max is not None and count > expected_max):
        raise WorkspaceE2EFixtureUnavailable()
    return count


def decode_fixture_cleanup_guard(
    payload: dict | bytes | str | E2EProjectCleanupGuard,
) -> bool:
    """Validate the closed guard result; return whether cleanup may proceed."""
    try:
        result = _CLEANUP_GUARD_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise WorkspaceE2EFixtureUnavailable() from None
    return result.ok


def e2e_fixture_schema() -> dict[str, Any]:
    """Return the reply schema and fixed Workspace fixture capability descriptor."""
    schema = _REPLY_ADAPTER.json_schema()
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": PROTOCOL,
            "$comment": (
                "Fixed capabilities for existing guarded E2E fixtures. These "
                "functions accept no arbitrary SQL, table, or predicate selectors."
            ),
            "x-native-capabilities": {
                "protocol": PROTOCOL,
                "postgres_schema": NATIVE_SCHEMA,
                "functions": [
                    {
                        "name": "create_project",
                        "signature": "aap_workspace_fixture_v1.create_project(jsonb)",
                        "arguments": [
                            {
                                "name": "p_project",
                                "postgres_type": "jsonb",
                                "json_schema": E2EProjectRecord.model_json_schema(),
                            }
                        ],
                        "result_postgres_type": "uuid",
                        "result_json_schema": {
                            "type": "string",
                            "format": "uuid",
                        },
                        "transaction_semantics": (
                            "Insert one Project with the exact supplied closed fields "
                            "and the API ORM's remaining defaults. The caller owns "
                            "fixture scoping, authorization, the transaction, and "
                            "commit; native constraints propagate."
                        ),
                    },
                    {
                        "name": "create_templates",
                        "signature": "aap_workspace_fixture_v1.create_templates(jsonb)",
                        "arguments": [
                            {
                                "name": "p_templates",
                                "postgres_type": "jsonb",
                                "json_schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/$defs/E2ETemplateRecord"},
                                },
                            }
                        ],
                        "result_postgres_type": "integer",
                        "result_json_schema": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "transaction_semantics": (
                            "Insert only the supplied closed ProjectTemplate records "
                            "with the API ORM's remaining defaults. The caller owns "
                            "fixture scoping, authorization, the transaction, and "
                            "commit; native constraints propagate."
                        ),
                    },
                    {
                        "name": "guard_project_cleanup",
                        "signature": "aap_workspace_fixture_v1.guard_project_cleanup(uuid[])",
                        "arguments": [
                            {
                                "name": "p_project_ids",
                                "postgres_type": "uuid[]",
                                "json_schema": TypeAdapter(
                                    list[uuid.UUID]
                                ).json_schema(),
                            }
                        ],
                        "result_postgres_type": "jsonb",
                        "result_model": "E2EProjectCleanupGuard",
                        "result_schema_ref": "#/$defs/E2EProjectCleanupGuard",
                        "transaction_semantics": (
                            "Lock existing Projects in UUID order with NOWAIT, then "
                            "call the existing video-deletion guard for each before "
                            "the caller removes any child rows. Return only the "
                            "closed video_execution_not_retired denial. Never "
                            "manufacture retirement proof. The caller owns rollback "
                            "and commit; lock and serialization errors propagate."
                        ),
                    },
                    {
                        "name": "delete_projects",
                        "signature": "aap_workspace_fixture_v1.delete_projects(uuid[])",
                        "arguments": [
                            {
                                "name": "p_project_ids",
                                "postgres_type": "uuid[]",
                                "json_schema": TypeAdapter(
                                    list[uuid.UUID]
                                ).json_schema(),
                            }
                        ],
                        "result_postgres_type": "integer",
                        "result_json_schema": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "transaction_semantics": (
                            "Recheck the existing video-deletion guard before "
                            "deleting only the exact supplied Project IDs. Raise "
                            "SQLSTATE 23514 when video execution is not retired. "
                            "The caller owns child cleanup, rollback, and commit."
                        ),
                    },
                    {
                        "name": "delete_templates",
                        "signature": "aap_workspace_fixture_v1.delete_templates(boolean, text[])",
                        "arguments": [
                            {
                                "name": "p_shared",
                                "postgres_type": "boolean",
                                "json_schema": {"type": "boolean"},
                            },
                            {
                                "name": "p_display_ids",
                                "postgres_type": "text[]",
                                "json_schema": TypeAdapter(
                                    list[StrictStr]
                                ).json_schema(),
                            },
                        ],
                        "result_postgres_type": "integer",
                        "result_json_schema": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "transaction_semantics": (
                            "Delete either the fixed shared E2E template prefix or "
                            "the exact supplied display IDs. No arbitrary predicate "
                            "is accepted. The caller owns cleanup ordering, rollback, "
                            "and commit."
                        ),
                    },
                ],
                "authorization_boundary": (
                    "The fixed fixture capabilities require a database name ending "
                    "in _test or _e2e and add no actor or role authorization."
                ),
                "transaction_boundary": (
                    "The caller owns the active transaction, fixture cleanup ordering, "
                    "commit, and rollback. These functions never commit or retry."
                ),
            },
            "x-postgresql-relations": {
                "aap_workspace_fixture_v1.templates": {
                    "columns": ["id", "display_id"],
                    "row_key": ["id"],
                    "read_only": True,
                    "security_barrier": True,
                    "database_guard": "database name ends in _test or _e2e",
                }
            },
        }
    )
    return schema


__all__ = [
    "E2EProjectCleanupGuard",
    "E2EProjectRecord",
    "E2ETemplateRecord",
    "NATIVE_SCHEMA",
    "PROTOCOL",
    "WorkspaceE2EFixtureUnavailable",
    "decode_fixture_cleanup_guard",
    "decode_fixture_count",
    "decode_fixture_project_id",
    "e2e_fixture_schema",
]
