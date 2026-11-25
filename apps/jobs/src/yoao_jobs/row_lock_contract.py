"""Fixed native Jobs row locks in the caller's transaction."""

from pydantic import TypeAdapter

from .lifecycle_contract import (
    ASYNC_JOB_FUNCTION_ROW_TYPES,
    ASYNC_JOB_ROW_COLUMNS,
    AsyncJobLifecycleRow,
    JobIdRequest,
)

JOBS_ROW_LOCK_PROTOCOL = "aap-jobs-row-locks/v1"
JOBS_ROW_LOCK_FUNCTIONS = {
    "update": "yoao_jobs_v1.read_job_for_update",
    "share_nowait": "yoao_jobs_v1.read_job_for_share_nowait",
    "update_nowait": "yoao_jobs_v1.read_job_for_update_nowait",
}
JOBS_EXECUTION_SNAPSHOT_PROTOCOL = "aap-jobs-execution-snapshot/v1"
JOBS_EXECUTION_SNAPSHOT_FUNCTION = "yoao_jobs_v1.read_job_execution_snapshot"


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
                "lock": {
                    "update": "FOR UPDATE",
                    "share_nowait": "FOR SHARE NOWAIT",
                    "update_nowait": "FOR UPDATE NOWAIT",
                }[mode],
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
    from ._sql_loader import read_jobs_sql

    return "\n".join(
        read_jobs_sql(name)
        for name in ("0328_jobs_row_locks.sql", "0415_export_job_nowait.sql")
    )
