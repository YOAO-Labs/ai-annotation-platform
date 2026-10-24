"""Native action idempotency in the caller's existing Jobs transaction."""

from importlib.resources import files
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictStr, TypeAdapter

from .lifecycle_contract import (
    ASYNC_JOB_FUNCTION_ROW_TYPES,
    ASYNC_JOB_ROW_COLUMNS,
    AsyncJobLifecycleRow,
)

JOBS_ACTION_IDEMPOTENCY_PROTOCOL = "aap-jobs-action-idempotency/v1"
JOBS_ACTION_IDEMPOTENCY_FUNCTIONS = {
    "lock": "aap_jobs_v1.lock_action_idempotency",
    "find": "aap_jobs_v1.find_action_idempotent_jobs",
}


class ActionIdempotencyLockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    action: StrictStr | None
    project_id: UUID | None
    actor_id: UUID | None
    key: StrictStr | None


class ActionIdempotencyQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: StrictStr | None
    project_id: UUID | None
    actor_id: UUID | None
    key: StrictStr | None


def jobs_action_idempotency_schema():
    row_return = (
        "TABLE("
        + ", ".join(
            f"{name} {ASYNC_JOB_FUNCTION_ROW_TYPES[name]}"
            for name in ASYNC_JOB_ROW_COLUMNS
        )
        + ")"
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": JOBS_ACTION_IDEMPOTENCY_PROTOCOL,
        "$defs": {
            model.__name__: TypeAdapter(model).json_schema()
            for model in (
                ActionIdempotencyLockRequest,
                ActionIdempotencyQueryRequest,
                AsyncJobLifecycleRow,
            )
        },
        "x-postgresql-functions": {
            JOBS_ACTION_IDEMPOTENCY_FUNCTIONS["lock"]: {
                "arguments": "text, uuid, uuid, text",
                "returns": "void",
                "volatility": "volatile",
                "parallel": "unsafe",
            },
            JOBS_ACTION_IDEMPOTENCY_FUNCTIONS["find"]: {
                "arguments": "text, uuid, uuid, text",
                "returns": row_return,
                "volatility": "volatile",
                "parallel": "unsafe",
            },
        },
        "x-row-columns": list(ASYNC_JOB_ROW_COLUMNS),
        "x-row-types": dict(ASYNC_JOB_FUNCTION_ROW_TYPES),
        "x-lock": {
            "key": "aap:data-manager:{action}:{project_id}:{actor_id}:{key}",
            "hash": "pg_catalog.hashtextextended(key,0)",
            "mode": "pg_advisory_xact_lock; wait until caller transaction ends",
            "autoflush": False,
            "nulls": "legacy Python None spelling",
        },
        "x-query": {
            "scope": "kind, current project_id, user_id, payload ->> data_manager_idempotency_key",
            "ordering": "created_at DESC, id DESC",
            "lock": "FOR UPDATE, waiting; every matching row locked; no LIMIT",
            "status": "all statuses; no additional business predicate",
            "nulls": "legacy SQLAlchemy equality-to-None IS NULL predicates",
            "binding": "mapped ORM select autoflush; scalar first row; cached identity retained without forced refresh",
        },
        "x-transactions": {
            "commit": False,
            "rollback": False,
            "retry": False,
            "errors": "original PostgreSQL SQLSTATE",
            "authorization": "existing caller authority and digest checks retained",
        },
        "x-security": {
            "functions": "security_definer; fixed pg_catalog,pg_temp/UTC/ISO; PUBLIC execute revoked",
            "private_table_rights": "none required by consumer; ordinary global SELECT remains until other readers migrate",
        },
    }


def jobs_action_idempotency_sql():
    return (
        files("aap_jobs").joinpath("sql/0332_jobs_action_idempotency.sql").read_text()
    )
