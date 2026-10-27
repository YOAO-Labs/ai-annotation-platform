"""Fixed native Jobs row locks in the caller's transaction."""

from importlib.resources import files

from pydantic import TypeAdapter

from .lifecycle_contract import (
    ASYNC_JOB_FUNCTION_ROW_TYPES,
    ASYNC_JOB_ROW_COLUMNS,
    AsyncJobLifecycleRow,
    JobIdRequest,
)

JOBS_ROW_LOCK_PROTOCOL = "aap-jobs-row-locks/v1"
JOBS_ROW_LOCK_FUNCTIONS = {
    "update": "aap_jobs_v1.read_job_for_update",
    "share_nowait": "aap_jobs_v1.read_job_for_share_nowait",
}
JOBS_EXECUTION_SNAPSHOT_PROTOCOL = "aap-jobs-execution-snapshot/v1"
JOBS_EXECUTION_SNAPSHOT_FUNCTION = "aap_jobs_v1.read_job_execution_snapshot"


def jobs_execution_snapshot_schema():
    """Immutable binding read that follows the caller's existing Job row read."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": JOBS_EXECUTION_SNAPSHOT_PROTOCOL,
        "x-postgresql-functions": [JOBS_EXECUTION_SNAPSHOT_FUNCTION + "(uuid)"],
        "x-postgresql-result": {"type": ["string", "null"], "format": "uuid"},
        "x-transactions": {
            "commit": False,
            "rollback": False,
            "lock_lifetime": "caller transaction",
            "missing_row": "null",
        },
        "x-security": {
            "functions": "security_definer; fixed search_path; PUBLIC revoked",
            "private_table_rights": "none required by the consumer",
            "caller_contract": "read or lock the Job row first through the Jobs capability",
        },
    }


def jobs_row_lock_schema():
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": JOBS_ROW_LOCK_PROTOCOL,
        "title": "AAP Jobs transactional row locks",
        "$defs": {
            model.__name__: TypeAdapter(model).json_schema()
            for model in (JobIdRequest, AsyncJobLifecycleRow)
        },
        "x-postgresql-functions": {
            function: {
                "arguments": "uuid",
                "returns": "TABLE("
                + ", ".join(
                    f"{name} {ASYNC_JOB_FUNCTION_ROW_TYPES[name]}"
                    for name in ASYNC_JOB_ROW_COLUMNS
                )
                + ")",
                "lock": "FOR UPDATE" if mode == "update" else "FOR SHARE NOWAIT",
                "volatility": "volatile",
            }
            for mode, function in JOBS_ROW_LOCK_FUNCTIONS.items()
        },
        "x-row-columns": list(ASYNC_JOB_ROW_COLUMNS),
        "x-row-types": dict(ASYNC_JOB_FUNCTION_ROW_TYPES),
        "x-transactions": {
            "commit": False,
            "rollback": False,
            "lock_lifetime": "caller transaction or savepoint rollback",
            "missing_row": "zero rows",
            "errors": "original PostgreSQL SQLSTATE; no retry or translation",
            "authorization": "caller revalidates current domain authority",
        },
        "x-security": {
            "functions": "security_definer; fixed search_path; PUBLIC revoked",
            "private_table_rights": "none required by the consumer",
        },
    }


def jobs_row_lock_sql():
    return files("aap_jobs").joinpath("sql/0328_jobs_row_locks.sql").read_text()
