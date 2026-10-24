"""Language-neutral contract for Jobs lifecycle writes and row access."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    field_validator,
)

JOBS_LIFECYCLE_PROTOCOL = "aap-jobs-lifecycle/v1"
ASYNC_JOB_CREATION_RELATION = "aap_jobs_v1.async_job_creation"
ASYNC_JOB_READ_FUNCTION = "aap_jobs_v1.read_job"
ASYNC_JOB_BY_EXECUTION_ID_FUNCTION = "aap_jobs_v1.jobs_by_execution_id"
ASYNC_JOB_VIDEO_SUMMARY_FUNCTION = "aap_jobs_v1.insert_video_summary"
ASYNC_JOB_APPLY_LIFECYCLE_FUNCTION = "aap_jobs_v1.apply_lifecycle"
ASYNC_JOB_DISPATCH_FAILED_FUNCTION = "aap_jobs_v1.mark_dispatch_failed"
ASYNC_JOB_CANCEL_BATCH_FUNCTION = "aap_jobs_v1.cancel_batch_predict_jobs"
ASYNC_JOB_DELETE_BATCH_FUNCTION = "aap_jobs_v1.delete_batch_predict_jobs"
ASYNC_JOB_DELETE_TERMINAL_FUNCTION = "aap_jobs_v1.delete_terminal_jobs_before"

ASYNC_JOB_ROW_COLUMNS = (
    "id",
    "kind",
    "project_id",
    "retained_project_id",
    "user_id",
    "status",
    "progress_pct",
    "payload",
    "result",
    "error_message",
    "celery_task_id",
    "started_at",
    "completed_at",
    "created_at",
    "updated_at",
)
ASYNC_JOB_ROW_TYPES = {
    "id": "uuid",
    "kind": "character varying(40)",
    "project_id": "uuid",
    "retained_project_id": "uuid",
    "user_id": "uuid",
    "status": "character varying(20)",
    "progress_pct": "integer",
    "payload": "jsonb",
    "result": "jsonb",
    "error_message": "text",
    "celery_task_id": "character varying(64)",
    "started_at": "timestamp with time zone",
    "completed_at": "timestamp with time zone",
    "created_at": "timestamp with time zone",
    "updated_at": "timestamp with time zone",
}
ASYNC_JOB_FUNCTION_ROW_TYPES = {
    **ASYNC_JOB_ROW_TYPES,
    "kind": "character varying",
    "status": "character varying",
    "celery_task_id": "character varying",
}

LIFECYCLE_OPERATIONS = (
    "mark_running",
    "bind_execution_id",
    "update_payload",
    "update_progress",
    "replace_result",
    "mark_complete",
    "mark_failed",
    "mark_cancelled",
    "request_cancel",
)
LifecycleOperation = Literal[
    "mark_running",
    "bind_execution_id",
    "update_payload",
    "update_progress",
    "replace_result",
    "mark_complete",
    "mark_failed",
    "mark_cancelled",
    "request_cancel",
]
RowField = Literal[
    "id",
    "kind",
    "project_id",
    "retained_project_id",
    "user_id",
    "status",
    "progress_pct",
    "payload",
    "result",
    "error_message",
    "celery_task_id",
    "started_at",
    "completed_at",
    "created_at",
    "updated_at",
]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LifecycleCommand(_ClosedModel):
    """One operation's observed reads and writes against the caller's row."""

    operation: LifecycleOperation
    reads: dict[RowField, Any]
    values: dict[RowField, Any]
    written_fields: list[RowField]
    arguments: dict[str, Any]


class LifecycleApplyRequest(_ClosedModel):
    """Closed envelope passed once per dirty row from the mapper flush hook."""

    protocol: Literal[JOBS_LIFECYCLE_PROTOCOL] = Field(alias="schema")
    commands: list[LifecycleCommand]
    projection_changes: dict[Literal["payload", "result"], Any] = Field(
        json_schema_extra={
            "propertyNames": {"enum": ["payload", "result"]},
        }
    )
    changed_fields: list[RowField]
    updated_at_override_present: StrictBool
    updated_at_override: StrictStr | None = Field(
        json_schema_extra={"format": "date-time"}
    )
    legacy_changes: dict[RowField, Any] = Field(
        json_schema_extra={
            "propertyNames": {"enum": list(ASYNC_JOB_ROW_COLUMNS)},
        }
    )

    @field_validator("projection_changes")
    @classmethod
    def _projection_keys_are_closed(cls, value):
        if set(value) - {"payload", "result"}:
            raise ValueError("projection changes may only contain payload and result")
        return value

    @field_validator("legacy_changes")
    @classmethod
    def _legacy_change_keys_are_mapped_fields(cls, value):
        if set(value) - set(ASYNC_JOB_ROW_COLUMNS):
            raise ValueError("legacy changes contain an unmapped async-job field")
        return value


class LifecycleApplyReply(_ClosedModel):
    applied: StrictBool
    updated_at: datetime | None


class AsyncJobLifecycleRow(_ClosedModel):
    """Fixed public row used to refresh the existing API ORM identity."""

    id: UUID
    kind: StrictStr = Field(max_length=40)
    project_id: UUID | None
    retained_project_id: UUID | None
    user_id: UUID | None
    status: StrictStr = Field(max_length=20)
    progress_pct: StrictInt
    payload: Any
    result: Any
    error_message: StrictStr | None
    celery_task_id: StrictStr | None = Field(max_length=64)
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class JobIdRequest(_ClosedModel):
    job_id: UUID


class ExecutionIdRequest(_ClosedModel):
    execution_id: StrictStr


class VideoSummaryInsertRequest(_ClosedModel):
    tracker_job_id: UUID
    kind: Literal["video_tracker", "video_correction"]
    user_id: UUID | None
    project_id: UUID | None
    payload: dict[str, Any]
    celery_task_id: StrictStr | None


class DispatchFailureRequest(_ClosedModel):
    job_id: UUID
    failed_at: datetime
    error: StrictStr


class BatchPredictCancelRequest(_ClosedModel):
    batch_id: UUID
    completed_at: datetime


class BatchPredictDeleteRequest(_ClosedModel):
    batch_id: UUID


class TerminalDeleteRequest(_ClosedModel):
    cutoff: datetime
    statuses: list[StrictStr]


def jobs_lifecycle_schema() -> dict[str, Any]:
    """Return the checked JSON Schema and native SQL capability inventory."""

    models = (
        LifecycleCommand,
        LifecycleApplyRequest,
        LifecycleApplyReply,
        AsyncJobLifecycleRow,
        JobIdRequest,
        ExecutionIdRequest,
        VideoSummaryInsertRequest,
        DispatchFailureRequest,
        BatchPredictCancelRequest,
        BatchPredictDeleteRequest,
        TerminalDeleteRequest,
    )
    definitions: dict[str, Any] = {}
    for model in models:
        model_schema = TypeAdapter(model).json_schema()
        definitions.update(model_schema.get("$defs", {}))
        definitions[model.__name__] = {
            key: value for key, value in model_schema.items() if key != "$defs"
        }

    row_return = (
        "TABLE("
        + ", ".join(
            f"{name} {ASYNC_JOB_FUNCTION_ROW_TYPES[name]}"
            for name in ASYNC_JOB_ROW_COLUMNS
        )
        + ")"
    )
    functions = {
        ASYNC_JOB_READ_FUNCTION: {"arguments": "uuid", "returns": row_return},
        ASYNC_JOB_BY_EXECUTION_ID_FUNCTION: {
            "arguments": "text",
            "returns": row_return,
        },
        ASYNC_JOB_VIDEO_SUMMARY_FUNCTION: {
            "arguments": "uuid, text, uuid, uuid, jsonb, text",
            "returns": "boolean",
        },
        ASYNC_JOB_APPLY_LIFECYCLE_FUNCTION: {
            "arguments": "uuid, jsonb",
            "returns": "TABLE(applied boolean, updated_at timestamp with time zone)",
        },
        ASYNC_JOB_DISPATCH_FAILED_FUNCTION: {
            "arguments": "uuid, timestamp with time zone, text",
            "returns": "boolean",
        },
        ASYNC_JOB_CANCEL_BATCH_FUNCTION: {
            "arguments": "uuid, timestamp with time zone",
            "returns": "uuid[]",
        },
        ASYNC_JOB_DELETE_BATCH_FUNCTION: {"arguments": "uuid", "returns": "uuid[]"},
        ASYNC_JOB_DELETE_TERMINAL_FUNCTION: {
            "arguments": "timestamp with time zone, text[]",
            "returns": "uuid[]",
        },
    }

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": JOBS_LIFECYCLE_PROTOCOL,
        "title": "AAP Jobs lifecycle and row capabilities",
        "description": (
            "Caller-owned Jobs operations and fixed async_jobs row projection. "
            "The API retains the borrowed transaction and ORM identity."
        ),
        "$defs": definitions,
        "x-postgresql-functions": functions,
        "x-creation-relation": {
            "name": ASYNC_JOB_CREATION_RELATION,
            "auto_updatable": True,
            "columns": list(ASYNC_JOB_ROW_COLUMNS),
            "check_option": (
                "strict inserts require pending status, zero progress, empty result, "
                "no terminal fields, and retained_project_id NULL"
            ),
            "legacy_insert_bypass": (
                "preserves the original initializer behavior only for callers that "
                "already have async_jobs table INSERT privilege"
            ),
            "initialization": {
                "status": "pending",
                "progress_pct": 0,
                "result": {},
                "started_at": None,
                "completed_at": None,
                "error_message": None,
                "retained_project_id": "NULL (database-managed)",
            },
        },
        "x-row-columns": list(ASYNC_JOB_ROW_COLUMNS),
        "x-row-types": dict(ASYNC_JOB_ROW_TYPES),
        "x-row-key": ["id"],
        "x-lifecycle": {
            "flush_envelope": {
                "required_fields": [
                    "schema",
                    "commands",
                    "projection_changes",
                    "changed_fields",
                    "updated_at_override_present",
                    "updated_at_override",
                    "legacy_changes",
                ],
                "command_fields": [
                    "operation",
                    "reads",
                    "values",
                    "written_fields",
                    "arguments",
                ],
                "operations": list(LIFECYCLE_OPERATIONS),
                "projection_fields": ["payload", "result"],
                "changed_fields": "net ORM field history, in stable mapper order",
                "updated_at": "database now(); explicit override requires original private column UPDATE and primary-key SELECT",
                "partial_write_prefixes": True,
                "arguments": "successful Python scalar conversions are normalized; unreached optional arguments are null",
                "jsonb_null": "preserve JSON null separately from SQL NULL",
            },
            "status_guards": {
                "mark_running": "cached pending only; execution ID fills if truthy and empty",
                "mark_complete": "skip cached cancelled/failed",
                "mark_failed": "skip cached completed/cancelled",
                "mark_cancelled": "caller-supplied allowed_statuses; null means unconditional",
                "request_cancel": "cached pending/running only; shallow payload merge",
            },
            "transactions": {
                "commit": False,
                "rollback": False,
                "flush": "caller controlled except original create/track flush phases",
                "state_source": "caller ORM snapshots; no fresh status re-read",
            },
            "legacy_changes": (
                "requires original invoker primary-key SELECT and UPDATE on every changed column; "
                "never authorizes through client JSON"
            ),
        },
        "x-security": {
            "functions": "security_definer with fixed search_path and PUBLIC revoked",
            "read_projection": "explicit 15-column row, never async_jobs composite ABI",
            "creation": "strict profile plus preexisting table-INSERT compatibility",
            "private_table_rights": "no blanket revocation in this capability slice",
        },
    }


def jobs_lifecycle_sql() -> str:
    """Read the versioned native SQL resource without requiring a package install."""

    from importlib.resources import files

    return (
        files("aap_jobs")
        .joinpath("sql/0327_jobs_lifecycle.sql")
        .read_text(encoding="utf-8")
    )


def decode_lifecycle_apply_request(value: Any) -> LifecycleApplyRequest:
    """Validate a closed lifecycle flush envelope without coercing JSON snapshots."""

    return LifecycleApplyRequest.model_validate(value)
