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
    return files("aap_jobs").joinpath("sql/0330_jobs_fixture_cleanup.sql").read_text()
