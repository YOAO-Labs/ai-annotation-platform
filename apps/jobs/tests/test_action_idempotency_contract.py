from uuid import uuid4

import pytest
from pydantic import ValidationError

from aap_jobs.action_idempotency_contract import (
    ActionIdempotencyLockRequest,
    ActionIdempotencyQueryRequest,
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
