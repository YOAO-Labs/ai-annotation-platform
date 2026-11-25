"""Portable contract for test-database-only Jobs fixture cleanup."""

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter

JOBS_FIXTURE_CLEANUP_PROTOCOL = "aap-jobs-fixture-cleanup/v1"
JOBS_FIXTURE_CLEANUP_FUNCTION = "yoao_jobs_v1.delete_fixture_project_jobs"


class FixtureProjectCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    project_ids: list[UUID]


FixtureCleanupCount = Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]


def jobs_fixture_cleanup_schema():
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": JOBS_FIXTURE_CLEANUP_PROTOCOL,
        "$defs": {
            "FixtureProjectCleanupRequest": FixtureProjectCleanupRequest.model_json_schema(),
            "FixtureCleanupCount": TypeAdapter(FixtureCleanupCount).json_schema(),
        },
        "x-postgresql-functions": {
            JOBS_FIXTURE_CLEANUP_FUNCTION: {
                "arguments": "uuid[]",
                "returns": "bigint",
                "volatility": "volatile",
                "parallel": "unsafe",
            }
        },
        "x-scope": "current project_id = ANY(project_ids); all kinds/statuses; retained scope excluded",
        "x-empty-scope": "zero rows; duplicate IDs do not duplicate affected rows",
        "x-security": {
            "database": "actual database name must end in _test or _e2e",
            "owner": "Jobs definer; PUBLIC execute revoked",
            "guard": "yoao_workspace_fixture_v1.guard_project_cleanup(uuid[])",
            "search_path": "pg_catalog,pg_temp",
        },
        "x-errors": {
            "42501": "non-test database or missing capability",
            "22023": "NULL/null-element scope or malformed Workspace guard",
            "23514": "video_execution_not_retired",
            "55P03": "project NOWAIT lock contention",
        },
        "x-transactions": {
            "commit": False,
            "rollback": False,
            "locks": "Workspace project/retirement guard retained in caller transaction",
            "triggers": "original DELETE constraints and triggers retained",
            "caller": "existing fixture gates, project selection and savepoint boundary",
        },
    }


def jobs_fixture_cleanup_sql():
    from ._sql_loader import read_jobs_sql

    return read_jobs_sql("0330_jobs_fixture_cleanup.sql")


def jobs_fixture_guard_sql():
    from ._sql_loader import read_jobs_sql

    return read_jobs_sql("0432_jobs_fixture_guard.sql")


def jobs_fixture_dataset_guard_sql():
    from ._sql_loader import read_jobs_sql

    return "\n-- aap-statement-break\n".join(
        read_jobs_sql(name)
        for name in (
            "0437_fixture_reclaim_job_scope.sql",
            "0443_fixture_dataset_retired_jobs.sql",
            "0444_image_export_dataset_scope.sql",
            "0456_prediction_dataset_scope.sql",
            "0465_worker_job_dataset_receipts.sql",
            "0465_retry_born_dataset_scope.sql",
            "0465_cross_frame_born_dataset_scope.sql",
            "0465_worker_job_dataset_scope.sql",
        )
    )


def jobs_fixture_presentation_sql():
    from ._sql_loader import read_jobs_sql

    return read_jobs_sql("0432_jobs_fixture_presentation.sql")


def jobs_fixture_guard_schema():
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "aap-jobs-fixture-guard/v1",
        "$defs": {
            "FixtureProjectCleanupRequest": FixtureProjectCleanupRequest.model_json_schema(),
            "FixtureCleanupCount": TypeAdapter(FixtureCleanupCount).json_schema(),
        },
        "x-postgresql-functions": {
            "yoao_jobs_v1.guard_fixture_project_jobs": {
                "arguments": "uuid[]",
                "returns": "void",
                "volatility": "volatile",
                "parallel": "unsafe",
            },
            "yoao_jobs_v1.fixture_dataset_job_scope": {
                "arguments": "uuid, text, jsonb, uuid, uuid",
                "returns": "jsonb",
                "volatility": "immutable",
                "parallel": "unsafe",
            },
            "yoao_jobs_v1.fixture_job_dataset_scope": {
                "arguments": "uuid, text, jsonb, uuid, uuid",
                "returns": "jsonb",
                "volatility": "volatile",
                "parallel": "unsafe",
            },
            "yoao_jobs_v1.guard_fixture_dataset_jobs": {
                "arguments": "uuid[]",
                "returns": "void",
                "volatility": "volatile",
                "parallel": "unsafe",
            },
            "yoao_assets_v1.fixture_task_dataset_scope": {
                "arguments": "uuid, jsonb",
                "returns": "uuid[]",
                "volatility": "stable",
                "parallel": "unsafe",
            },
            "yoao_jobs_v1.create_filter_fixture_jobs": {
                "arguments": "uuid, uuid, uuid, uuid, uuid, uuid, text, text",
                "returns": "bigint",
                "volatility": "volatile",
                "parallel": "unsafe",
            },
            "yoao_jobs_v1.delete_fixture_presentation_jobs": {
                "arguments": "uuid, uuid, uuid[]",
                "returns": "bigint",
                "volatility": "volatile",
                "parallel": "unsafe",
            },
            JOBS_FIXTURE_CLEANUP_FUNCTION: {
                "arguments": "uuid[]",
                "returns": "bigint",
                "volatility": "volatile",
                "parallel": "unsafe",
            },
        },
        "x-scope": "exact sealed fixture runs own project and typed dataset Jobs; image exports, batch predictions, verified retry admissions and cross-frame Jobs retain their born Dataset sets; unknown scope or matching unsettled work blocks cleanup even after a bound worker Job is deleted; scope alone is not physical retirement",
        "x-empty-scope": "guard succeeds and deletion returns zero",
        "x-security": {
            "database": "actual database name must end in _test or _e2e",
            "owner": "Jobs definer; PUBLIC execute revoked",
            "guard": "yoao_workspace_fixture_v1.guard_project_cleanup(uuid[])",
            "search_path": "pg_catalog,pg_temp",
        },
        "x-errors": {
            "0A000": "fixture Dataset cleanup requires READ COMMITTED isolation",
            "42501": "non-test database or missing capability",
            "22023": "NULL/null-element scope or malformed Workspace guard",
            "23514": "video_execution_not_retired, fixture_job_execution_unsettled or fixture_dataset_job_unsettled",
            "55P03": "project or image export Dataset scope contention",
        },
        "x-transactions": {
            "commit": False,
            "rollback": False,
            "isolation": "READ COMMITTED for Dataset cleanup",
            "locks": "Workspace project/retirement guard retained in caller transaction",
            "triggers": "original DELETE constraints and triggers retained",
        },
    }
