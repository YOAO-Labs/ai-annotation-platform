"""Portable contract for test-database-only Jobs fixture cleanup."""

from importlib.resources import files
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter

JOBS_FIXTURE_CLEANUP_PROTOCOL = "aap-jobs-fixture-cleanup/v1"
JOBS_FIXTURE_CLEANUP_FUNCTION = "aap_jobs_v1.delete_fixture_project_jobs"


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
            "guard": "aap_workspace_fixture_v1.guard_project_cleanup(uuid[])",
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
    return files("yoao_jobs").joinpath("sql/0330_jobs_fixture_cleanup.sql").read_text()


def jobs_fixture_guard_sql():
    return files("yoao_jobs").joinpath("sql/0432_jobs_fixture_guard.sql").read_text()


def jobs_fixture_dataset_guard_sql():
    sql = files("yoao_jobs").joinpath("sql")
    return "\n-- aap-statement-break\n".join(
        sql.joinpath(name).read_text()
        for name in (
            "0437_fixture_reclaim_job_scope.sql",
            "0443_fixture_dataset_retired_jobs.sql",
            "0444_image_export_dataset_scope.sql",
        )
    )


def jobs_fixture_presentation_sql():
    return (
        files("yoao_jobs")
        .joinpath("sql/0432_jobs_fixture_presentation.sql")
        .read_text()
    )


def jobs_fixture_guard_schema():
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "aap-jobs-fixture-guard/v1",
        "$defs": {
            "FixtureProjectCleanupRequest": FixtureProjectCleanupRequest.model_json_schema(),
            "FixtureCleanupCount": TypeAdapter(FixtureCleanupCount).json_schema(),
        },
        "x-postgresql-functions": {
            "aap_jobs_v1.guard_fixture_project_jobs": {
                "arguments": "uuid[]",
                "returns": "void",
                "volatility": "volatile",
                "parallel": "unsafe",
            },
            "aap_jobs_v1.fixture_dataset_job_scope": {
                "arguments": "uuid, text, jsonb, uuid, uuid",
                "returns": "jsonb",
                "volatility": "immutable",
                "parallel": "unsafe",
            },
            "aap_jobs_v1.fixture_job_dataset_scope": {
                "arguments": "uuid, text, jsonb, uuid, uuid",
                "returns": "jsonb",
                "volatility": "volatile",
                "parallel": "unsafe",
            },
            "aap_jobs_v1.guard_fixture_dataset_jobs": {
                "arguments": "uuid[]",
                "returns": "void",
                "volatility": "volatile",
                "parallel": "unsafe",
            },
            "aap_jobs_v1.create_filter_fixture_jobs": {
                "arguments": "uuid, uuid, uuid, uuid, uuid, uuid, text, text",
                "returns": "bigint",
                "volatility": "volatile",
                "parallel": "unsafe",
            },
            "aap_jobs_v1.delete_fixture_presentation_jobs": {
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
        "x-scope": "exact sealed fixture runs own project and typed dataset Jobs; immutable image export bindings retain their exact Dataset sets across Job deletion; unproved unknown scopes block cleanup; no-dispatch presentation receipts or matching execution-owner retirement proofs are required",
        "x-empty-scope": "guard succeeds and deletion returns zero",
        "x-security": {
            "database": "actual database name must end in _test or _e2e",
            "owner": "Jobs definer; PUBLIC execute revoked",
            "guard": "aap_workspace_fixture_v1.guard_project_cleanup(uuid[])",
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
