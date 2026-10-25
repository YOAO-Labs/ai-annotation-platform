"""Closed contracts for the trusted local scale-seed workflow."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal
import uuid

from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from aap_workspace_contract.wire import ClosedModel, _json_bytes

PROTOCOL = "aap-workspace-scale-seed/v1"
SOURCE_GUARD_PROTOCOL = "aap-workspace-scale-source-guard/v1"
NATIVE_SCHEMA = "aap_workspace_v1"
ASSETS_NATIVE_SCHEMA = "aap_assets_v1"


class _ScaleSeedModel(ClosedModel):
    @model_validator(mode="before")
    @classmethod
    def use_declared_schema_field(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class ScaleSeedError(_ScaleSeedModel):
    """Closed code returned by a retention guard."""

    code: StrictStr = Field(min_length=1)


class ScaleSeedCleanup(_ScaleSeedModel):
    """Captured Project IDs and the result of project/source retention checks."""

    protocol: Literal["aap-workspace-scale-seed/v1"] = Field(alias="schema")
    marker: StrictStr = Field(min_length=1)
    project_ids: list[uuid.UUID]
    ok: StrictBool
    error: ScaleSeedError | None

    @model_validator(mode="after")
    def result_matches_error(self) -> ScaleSeedCleanup:
        if self.ok != (self.error is None):
            raise ValueError("Scale seed cleanup result/error mismatch")
        if self.project_ids != sorted(set(self.project_ids)):
            raise ValueError("Scale seed Project IDs must be unique and sorted")
        return self


class ScaleSeedSourceGuard(_ScaleSeedModel):
    """Closed Dataset source-retention guard result."""

    protocol: Literal["aap-workspace-scale-source-guard/v1"] = Field(alias="schema")
    dataset_id: uuid.UUID
    after_task_cleanup: StrictBool
    ok: StrictBool
    error: ScaleSeedError | None

    @model_validator(mode="after")
    def result_matches_error(self) -> ScaleSeedSourceGuard:
        if self.ok != (self.error is None):
            raise ValueError("Scale seed source guard result/error mismatch")
        return self


class WorkspaceScaleSeedUnavailable(RuntimeError):
    """A fixed scale-seed capability is unavailable or returned malformed data."""

    def __init__(self):
        super().__init__("Workspace scale seed capability unavailable")


_UUID_ADAPTER = TypeAdapter(uuid.UUID)
_COUNT_ADAPTER = TypeAdapter(StrictInt)
_REPLY_ADAPTER = TypeAdapter(ScaleSeedCleanup | ScaleSeedSourceGuard)
_CLEANUP_ADAPTER = TypeAdapter(ScaleSeedCleanup)
_SOURCE_GUARD_ADAPTER = TypeAdapter(ScaleSeedSourceGuard)
_MARKER_ADAPTER = TypeAdapter(StrictStr)
_UUIDS_ADAPTER = TypeAdapter(list[uuid.UUID])
_BOOL_ADAPTER = TypeAdapter(StrictBool)


def decode_scale_seed_project_id(payload: Any) -> uuid.UUID:
    """Validate a scalar UUID result in UUID or canonical string form."""
    try:
        if isinstance(payload, uuid.UUID):
            return payload
        if type(payload) is not str:
            raise ValueError("invalid UUID result")
        project_id = _UUID_ADAPTER.validate_python(payload)
        if str(project_id) != payload:
            raise ValueError("non-canonical UUID")
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise WorkspaceScaleSeedUnavailable() from None
    return project_id


def decode_scale_seed_count(payload: Any, maximum: int | None = None) -> int:
    """Validate a nonnegative native row count and optional caller-known maximum."""
    if maximum is not None and (type(maximum) is not int or maximum < 0):
        raise WorkspaceScaleSeedUnavailable()
    try:
        count = _COUNT_ADAPTER.validate_python(payload)
    except (ValidationError, ValueError, TypeError):
        raise WorkspaceScaleSeedUnavailable() from None
    if count < 0 or (maximum is not None and count > maximum):
        raise WorkspaceScaleSeedUnavailable()
    return count


def decode_scale_seed_cleanup(
    payload: dict | bytes | str | ScaleSeedCleanup,
    *,
    marker: str,
) -> ScaleSeedCleanup:
    """Validate the closed preflight reply and bind it to its requested marker."""
    if type(marker) is not str or not marker.strip():
        raise WorkspaceScaleSeedUnavailable()
    try:
        result = _CLEANUP_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise WorkspaceScaleSeedUnavailable() from None
    if result.marker != marker:
        raise WorkspaceScaleSeedUnavailable()
    return result


def decode_scale_seed_source_guard(
    payload: dict | bytes | str | ScaleSeedSourceGuard,
    *,
    dataset_id: uuid.UUID,
    after_task_cleanup: bool,
) -> ScaleSeedSourceGuard:
    """Validate and bind a Dataset source-retention guard reply."""
    if not isinstance(dataset_id, uuid.UUID) or type(after_task_cleanup) is not bool:
        raise WorkspaceScaleSeedUnavailable()
    try:
        result = _SOURCE_GUARD_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise WorkspaceScaleSeedUnavailable() from None
    if (
        result.dataset_id != dataset_id
        or result.after_task_cleanup is not after_task_cleanup
    ):
        raise WorkspaceScaleSeedUnavailable()
    return result


_NATIVE_FUNCTIONS = (
    {
        "name": "create_scale_seed_project",
        "owner_domain": "Workspace",
        "arguments": (
            ("p_owner_id", "uuid", _UUID_ADAPTER),
            ("p_marker", "text", _MARKER_ADAPTER),
        ),
        "result_postgres_type": "uuid",
        "result_json_schema": {"type": "string", "format": "uuid"},
        "transaction_semantics": (
            "Insert the original scale-seed Project fields while preserving all other "
            "PostgreSQL defaults. The caller owns the transaction and writes the "
            "Labeling total_tasks projection before commit."
        ),
    },
    {
        "name": "ensure_scale_seed_member",
        "owner_domain": "Workspace",
        "arguments": (
            ("p_project_id", "uuid", _UUID_ADAPTER),
            ("p_annotator_id", "uuid", _UUID_ADAPTER),
            ("p_owner_id", "uuid", _UUID_ADAPTER),
        ),
        "result_postgres_type": "integer",
        "result_json_schema": {"type": "integer", "minimum": 0, "maximum": 1},
        "transaction_semantics": (
            "Insert the original annotator membership with ON CONFLICT DO NOTHING "
            "and return the inserted row count. No account or role normalization is "
            "performed; the caller owns the transaction."
        ),
    },
    {
        "name": "guard_scale_seed_cleanup",
        "owner_domain": "Workspace",
        "arguments": (("p_marker", "text", _MARKER_ADAPTER),),
        "result_postgres_type": "jsonb",
        "result_model": "ScaleSeedCleanup",
        "result_schema_ref": "#/$defs/ScaleSeedCleanup",
        "transaction_semantics": (
            "Capture Projects matching the marker, acquire sorted Learning project "
            "advisory locks before sorted Workspace row locks with NOWAIT, and run "
            "Learning and video-retirement guards before child DML. The caller owns "
            "the transaction and rollback. No retirement proof is fabricated."
        ),
    },
    {
        "name": "delete_scale_seed_projects",
        "owner_domain": "Workspace",
        "arguments": (
            ("p_marker", "text", _MARKER_ADAPTER),
            ("p_project_ids", "uuid[]", _UUIDS_ADAPTER),
        ),
        "result_postgres_type": "integer",
        "result_json_schema": {"type": "integer", "minimum": 0},
        "transaction_semantics": (
            "Delete only the supplied captured IDs whose current name still matches "
            "the marker. Recheck Learning and video-retirement guards before DML; "
            "raise SQLSTATE 23514 with a code detail when retained. The caller owns "
            "the transaction."
        ),
    },
    {
        "name": "guard_scale_seed_dataset_cleanup",
        "owner_domain": "Assets",
        "native_schema": ASSETS_NATIVE_SCHEMA,
        "arguments": (
            ("p_dataset_id", "uuid", _UUID_ADAPTER),
            ("p_after_task_cleanup", "boolean", _BOOL_ADAPTER),
        ),
        "result_postgres_type": "jsonb",
        "result_model": "ScaleSeedSourceGuard",
        "result_schema_ref": "#/$defs/ScaleSeedSourceGuard",
        "transaction_semantics": (
            "Hold the private Assets Dataset-to-Scene-to-Item NOWAIT locks while "
            "checking Jobs, Learning, and Assets retention. When after_task_cleanup "
            "is true, also check Labeling retention. This guard performs no source "
            "DML; the caller owns the transaction and lock lifetime."
        ),
    },
)


def scale_seed_schema() -> dict[str, Any]:
    """Return the reply JSON Schema and the five fixed owner capabilities."""
    schema = _REPLY_ADAPTER.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = PROTOCOL
    schema["$comment"] = (
        "Fixed trusted local scale-seed capabilities. Functions accept no arbitrary "
        "SQL, table, or predicate selectors."
    )
    schema["x-native-capabilities"] = {
        "protocol": PROTOCOL,
        "postgres_schema": NATIVE_SCHEMA,
        "owner_domains": {
            "Workspace": NATIVE_SCHEMA,
            "Assets": ASSETS_NATIVE_SCHEMA,
            "Jobs": "aap_jobs_v1",
            "Learning": "aap_learning_v1",
            "Labeling": "aap_labeling_v1",
        },
        "functions": [
            {
                "name": function["name"],
                "signature": (
                    f"{function.get('native_schema', NATIVE_SCHEMA)}."
                    f"{function['name']}"
                    f"({', '.join(argument[1] for argument in function['arguments'])})"
                ),
                "owner_domain": function["owner_domain"],
                "arguments": [
                    {
                        "name": name,
                        "postgres_type": postgres_type,
                        "json_schema": deepcopy(adapter.json_schema()),
                    }
                    for name, postgres_type, adapter in function["arguments"]
                ],
                **{
                    key: deepcopy(value)
                    for key, value in function.items()
                    if key
                    in (
                        "result_postgres_type",
                        "result_json_schema",
                        "result_model",
                        "result_schema_ref",
                        "transaction_semantics",
                    )
                },
            }
            for function in _NATIVE_FUNCTIONS
        ],
        "delegated_guards": [
            {
                "owner_domain": "Learning",
                "signature": "aap_learning_v1.lock_project_deletion(uuid)",
            },
            {
                "owner_domain": "Learning",
                "signature": "aap_learning_v1.guard_project_deletion(uuid)",
            },
            {
                "owner_domain": "Labeling",
                "signature": "aap_labeling_v1.guard_project_video_deletion(uuid)",
            },
            {
                "owner_domain": "Assets",
                "signature": "aap_assets_v1._lock_dataset_source_deletion(uuid,uuid)",
                "private": True,
            },
            {
                "owner_domain": "Jobs",
                "signature": "aap_jobs_v1.guard_dataset_source_deletion(uuid)",
            },
            {
                "owner_domain": "Learning",
                "signature": "aap_learning_v1.guard_dataset_source_deletion(uuid,uuid)",
            },
            {
                "owner_domain": "Assets",
                "signature": "aap_assets_v1.guard_dataset_source_auxiliary_retention(uuid,uuid)",
            },
            {
                "owner_domain": "Labeling",
                "signature": "aap_labeling_v1.guard_dataset_source_deletion(uuid,uuid)",
            },
        ],
        "authorization_boundary": (
            "Available only through a separately granted trusted operator. The CLI "
            "retains its production guard before engine creation; these fixed "
            "functions add no application actor policy."
        ),
        "transaction_boundary": (
            "The caller owns the encompassing transaction, all commits and rollbacks, "
            "and the lifetime of locks. No function commits or retries."
        ),
    }
    return schema


__all__ = [
    "ASSETS_NATIVE_SCHEMA",
    "NATIVE_SCHEMA",
    "PROTOCOL",
    "SOURCE_GUARD_PROTOCOL",
    "ScaleSeedCleanup",
    "ScaleSeedSourceGuard",
    "WorkspaceScaleSeedUnavailable",
    "decode_scale_seed_cleanup",
    "decode_scale_seed_count",
    "decode_scale_seed_project_id",
    "decode_scale_seed_source_guard",
    "scale_seed_schema",
]
