"""Frozen query ABI and language-neutral request boundaries."""

import json
from pathlib import Path
import uuid

import pytest
from pydantic import ValidationError

from aap_jobs.domain_query_contract import (
    CrossFramePageRequest,
    JobCount,
    jobs_domain_query_schema,
    jobs_domain_query_sql,
)


def test_domain_query_resources_match_the_frozen_contract():
    root = Path(__file__).resolve().parents[3]
    schema = jobs_domain_query_schema()
    assert (
        json.loads(
            (root / "apps/jobs/src/aap_jobs/domain_queries.schema.json").read_text()
        )
        == schema
    )
    assert (
        json.loads(
            (
                root / "apps/api/alembic/sql/0329_jobs_domain_queries.schema.json"
            ).read_text()
        )
        == schema
    )
    assert (
        root / "apps/api/alembic/sql/0329_jobs_domain_queries.sql"
    ).read_text() == jobs_domain_query_sql()
    assert len(schema["x-postgresql-functions"]) == 5
    assert len(schema["x-row-columns"]) == 15


def test_page_request_is_closed_and_keeps_scene_text():
    value = {
        "actor_id": str(uuid.uuid4()),
        "scene_id": "Scene-原值-ABC",
        "limit": 50,
    }
    request = CrossFramePageRequest.model_validate_json(json.dumps(value))
    assert request.scene_id == value["scene_id"]
    for invalid in [None, True, 0, 51, "20"]:
        with pytest.raises(ValidationError):
            CrossFramePageRequest.model_validate_json(
                json.dumps({**value, "limit": invalid})
            )
    with pytest.raises(ValidationError):
        CrossFramePageRequest.model_validate_json(json.dumps({**value, "extra": 1}))


def test_count_preserves_signed_bigint_without_boolean_or_string_coercion():
    assert JobCount.model_validate_json('{"count":9223372036854775807}').count == (
        2**63 - 1
    )
    for invalid in [-1, 2**63, True, "3"]:
        with pytest.raises(ValidationError):
            JobCount.model_validate_json(json.dumps({"count": invalid}))
