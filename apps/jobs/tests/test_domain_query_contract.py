"""Frozen query ABI and language-neutral request boundaries."""

import json
import uuid

import pytest
from pydantic import ValidationError

from aap_jobs.domain_query_contract import (
    CrossFramePageRequest,
    FrozenDeploymentUsage,
    FrozenDeploymentUsageRequest,
    JobCount,
)


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


def test_frozen_deployment_usage_is_closed_and_requires_real_bigints():
    ids = {key: str(uuid.uuid4()) for key in ("deployment_id", "registry_id", "model_version_id")}
    request = FrozenDeploymentUsageRequest.model_validate_json(json.dumps(ids))
    assert str(request.deployment_id) == ids["deployment_id"]
    with pytest.raises(ValidationError):
        FrozenDeploymentUsageRequest.model_validate_json(json.dumps({**ids, "extra": 1}))
    usage = FrozenDeploymentUsage.model_validate({"active_jobs": 1, "retryable_failures": 2})
    assert usage.active_jobs == 1 and usage.retryable_failures == 2
    for value in (True, "1", -1, 2**63):
        with pytest.raises(ValidationError):
            FrozenDeploymentUsage.model_validate({"active_jobs": value, "retryable_failures": 0})
