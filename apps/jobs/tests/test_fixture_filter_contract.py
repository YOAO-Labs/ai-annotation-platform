from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from aap_jobs.fixture_filter_contract import (
    FilterFixtureJobsCount,
    FilterFixtureJobsRequest,
)


def test_filter_fixture_requires_exact_distinct_job_ids():
    completed, failed = uuid4(), uuid4()
    request = FilterFixtureJobsRequest(completed_job_id=completed, failed_job_id=failed)
    assert request.completed_job_id == completed and request.failed_job_id == failed
    for value in (
        {"completed_job_id": completed, "failed_job_id": completed},
        {"completed_job_id": completed, "failed_job_id": None},
        {"completed_job_id": completed, "failed_job_id": failed, "all": True},
    ):
        with pytest.raises(ValidationError):
            FilterFixtureJobsRequest.model_validate(value)


def test_filter_fixture_count_rejects_partial_or_coerced_outcomes():
    count = TypeAdapter(FilterFixtureJobsCount)
    assert count.validate_python(2) == 2
    for value in (0, 1, 3, True, 2.0, "2", None):
        with pytest.raises(ValidationError):
            count.validate_python(value)
