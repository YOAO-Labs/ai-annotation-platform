import json
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aap_jobs.action_idempotency_contract import (
    ActionIdempotencyLockRequest,
    ActionIdempotencyQueryRequest,
    JOBS_ACTION_IDEMPOTENCY_FUNCTIONS,
    jobs_action_idempotency_schema,
    jobs_action_idempotency_sql,
)
from aap_jobs.lifecycle_contract import (
    ASYNC_JOB_ROW_COLUMNS,
    ASYNC_JOB_FUNCTION_ROW_TYPES,
)


def test_action_idempotency_requests_preserve_opaque_values_and_legacy_nulls():
    actor, project = uuid4(), uuid4()
    key = "  中文:key:%s\\'  "
    query = ActionIdempotencyQueryRequest(
        kind="future_kind", project_id=project, actor_id=actor, key=key
    )
    assert query.key == key and query.actor_id == actor
    assert (
        ActionIdempotencyLockRequest(
            action="predict", project_id=project, actor_id=actor, key=key
        ).key
        == key
    )
    assert (
        ActionIdempotencyQueryRequest(
            kind=None, project_id=None, actor_id=None, key=None
        ).key
        is None
    )
    assert (
        ActionIdempotencyLockRequest(
            action=None, project_id=None, actor_id=None, key=None
        ).action
        is None
    )


def test_action_idempotency_request_fields_are_closed_and_not_coerced():
    common = {
        "kind": "batch_predict",
        "project_id": uuid4(),
        "actor_id": uuid4(),
        "key": "key",
    }
    for change in ({"key": True}, {"actor_id": "not-a-uuid"}, {"limit": 1}):
        with pytest.raises(ValidationError):
            ActionIdempotencyQueryRequest.model_validate({**common, **change})
    with pytest.raises(ValidationError):
        ActionIdempotencyQueryRequest.model_validate({"kind": "batch_predict"})


def test_action_idempotency_schema_and_frozen_sql_match_published_row_abi():
    root = Path(__file__).resolve().parents[3]
    schema = jobs_action_idempotency_schema()
    assert schema["x-row-columns"] == list(ASYNC_JOB_ROW_COLUMNS)
    assert schema["x-row-types"] == dict(ASYNC_JOB_FUNCTION_ROW_TYPES)
    assert len(schema["x-postgresql-functions"]) == 2
    assert (
        schema["x-postgresql-functions"][JOBS_ACTION_IDEMPOTENCY_FUNCTIONS["lock"]][
            "returns"
        ]
        == "void"
    )
    assert (
        json.loads(
            files("aap_jobs").joinpath("action_idempotency.schema.json").read_text()
        )
        == schema
    )
    assert (
        json.loads(
            (
                root / "apps/api/alembic/sql/0332_jobs_action_idempotency.schema.json"
            ).read_text()
        )
        == schema
    )
    assert (
        root / "apps/api/alembic/sql/0332_jobs_action_idempotency.sql"
    ).read_text() == jobs_action_idempotency_sql()
