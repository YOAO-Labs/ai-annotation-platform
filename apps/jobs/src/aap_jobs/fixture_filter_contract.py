"""Fixed test-only filtering-fixture Jobs projections."""

from importlib.resources import files
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    TypeAdapter,
    model_validator,
)

JOBS_FILTER_FIXTURE_PROTOCOL = "aap-jobs-filter-fixtures/v1"
JOBS_FILTER_FIXTURE_FUNCTION = "aap_jobs_v1.set_filter_fixture_job_outcomes"


class FilterFixtureJobsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    completed_job_id: UUID
    failed_job_id: UUID

    @model_validator(mode="after")
    def distinct_ids(self):
        if self.completed_job_id == self.failed_job_id:
            raise ValueError("filter fixture job IDs must be distinct")
        return self


FilterFixtureJobsCount = Annotated[StrictInt, Field(ge=2, le=2)]


def jobs_filter_fixture_schema():
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": JOBS_FILTER_FIXTURE_PROTOCOL,
        "$defs": {
            "FilterFixtureJobsRequest": FilterFixtureJobsRequest.model_json_schema(),
            "FilterFixtureJobsCount": TypeAdapter(FilterFixtureJobsCount).json_schema(),
        },
        "x-postgresql-functions": {
            JOBS_FILTER_FIXTURE_FUNCTION: {
                "arguments": "uuid, uuid",
                "returns": "bigint",
                "volatility": "volatile",
                "parallel": "unsafe",
            },
        },
        "x-projections": {
            "completed": {
                "status": "completed",
                "progress_pct": 100,
                "result": {"success_count": 2},
            },
            "failed": {
                "status": "failed",
                "progress_pct": 40,
                "error_message": "fixture failure",
            },
        },
        "x-security": {
            "database": "actual database name must end in _test or _e2e",
            "owner": "Jobs definer; PUBLIC execute revoked",
            "search_path": "pg_catalog,pg_temp",
        },
        "x-errors": {
            "42501": "non-test database or missing capability",
            "22023": "NULL or duplicate job IDs",
            "P0002": "a selected job row does not exist",
        },
        "x-transactions": {
            "commit": False,
            "rollback": False,
            "ordering": "ascending UUID, same as the original mapper updates",
            "masks": "only the listed projection fields plus updated_at=now(); original UPDATE OF triggers retained",
            "preserved": "all other fields, including started_at/completed_at and failed result",
            "atomic": "missing second row rolls back the entire function statement",
        },
    }


def jobs_filter_fixture_sql():
    return files("aap_jobs").joinpath("sql/0331_jobs_filter_fixtures.sql").read_text()
