import json
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from aap_jobs.fixture_cleanup_contract import (
    FixtureCleanupCount,
    FixtureProjectCleanupRequest,
    jobs_fixture_cleanup_schema,
    jobs_fixture_cleanup_sql,
)


def test_fixture_scope_is_closed_and_preserves_empty_or_duplicate_ids():
    first = uuid4()
    assert FixtureProjectCleanupRequest(project_ids=[]).project_ids == []
    assert FixtureProjectCleanupRequest(project_ids=[first, first]).project_ids == [
        first,
        first,
    ]
    for value in (
        {"project_ids": None},
        {"project_ids": [None]},
        {"project_ids": [first], "all": True},
    ):
        with pytest.raises(ValidationError):
            FixtureProjectCleanupRequest.model_validate(value)


def test_fixture_count_preserves_native_nonnegative_int64_boundaries():
    count = TypeAdapter(FixtureCleanupCount)
    assert count.validate_python(0) == 0
    assert count.validate_python(2**63 - 1) == 2**63 - 1
    for value in (None, True, -1, 1.0, "1", 2**63):
        with pytest.raises(ValidationError):
            count.validate_python(value)


def test_packaged_fixture_resources_match_schema_and_migration():
    root = Path(__file__).resolve().parents[3]
    resource = files("aap_jobs")
    assert (
        json.loads(resource.joinpath("fixture_cleanup.schema.json").read_text())
        == jobs_fixture_cleanup_schema()
    )
    assert (
        json.loads(
            (
                root / "apps/api/alembic/sql/0330_jobs_fixture_cleanup.schema.json"
            ).read_text()
        )
        == jobs_fixture_cleanup_schema()
    )
    assert (
        root / "apps/api/alembic/sql/0330_jobs_fixture_cleanup.sql"
    ).read_text() == jobs_fixture_cleanup_sql()
