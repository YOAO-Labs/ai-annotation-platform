from __future__ import annotations

import json

import httpx
import pytest

from ai_annotation.errors import (
    AuthenticationError,
    ConflictError,
    PermissionDeniedError,
    ValidationError,
)
from ai_annotation.model_management import LearningPolicies
from ai_annotation.model_management_models import LearningPolicySaveRequest
from ai_annotation.training_models import TrainingConfiguration

from .conftest import API

PROJECT_ID = "00000000-0000-0000-0000-000000000011"
PARENT_ID = "00000000-0000-0000-0000-000000000012"
POLICY_ID = "00000000-0000-0000-0000-000000000013"
REQUEST_ID = "00000000-0000-0000-0000-000000000014"


def policy_json(**updates):
    return {
        "id": POLICY_ID,
        "project_id": PROJECT_ID,
        "creator_id": None,
        "revision": 4,
        "enabled": False,
        "config": {"parent_id": PARENT_ID, "parameters": {"epochs": 50}},
        "initialization_model_id": None,
        "baseline_dataset_id": PARENT_ID,
        "blocked_train_digest": None,
        "last_checked_at": "2026-10-16T00:00:00Z",
        "next_check_at": None,
        "last_started_at": None,
        "state": "disabled",
        "message": "Automatic learning is disabled",
        "future_extension": "kept",
        **updates,
    }


def test_learning_policy_operations_and_request_serialization(client, respx_mock):
    api = LearningPolicies(client._http)
    get_route = respx_mock.get(f"{API}/projects/{PROJECT_ID}/learning-policy").mock(
        return_value=httpx.Response(
            200, content=b"null", headers={"Content-Type": "application/json"}
        )
    )
    assert api.get(PROJECT_ID) is None
    assert get_route.called

    config = TrainingConfiguration(
        schema_id="ultralytics.yolo11n.v1",
        schema_digest="a" * 64,
        values={"epochs": 24},
    )
    save_route = respx_mock.put(f"{API}/projects/{PROJECT_ID}/learning-policy").mock(
        return_value=httpx.Response(200, json=policy_json(revision=5))
    )
    saved = api.save(
        PROJECT_ID,
        LearningPolicySaveRequest(
            expected_revision=4,
            enabled=True,
            parent_id=PARENT_ID,
            batch_id=None,
            framework="ultralytics",
            base_model="yolo11n",
            configuration=config,
            initialization=None,
            origin=None,
            min_changed_samples=20,
            check_interval_minutes=60,
            cooldown_minutes=30,
        ),
    )
    assert saved.revision == 5
    assert saved.model_extra["future_extension"] == "kept"
    body = json.loads(save_route.calls.last.request.content)
    assert body["configuration"] == config.model_dump(mode="json")
    assert body["batch_id"] is None
    assert body["initialization"] is None
    assert body["origin"] is None
    assert "parameters" not in body

    check_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/learning-policy/check"
    ).mock(return_value=httpx.Response(200, json=policy_json(state="waiting")))
    checked = api.check(PROJECT_ID)
    assert checked.state == "waiting"
    assert check_route.calls.last.request.content in (b"", b"null")

    disable_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/learning-policy/disable"
    ).mock(return_value=httpx.Response(200, json=policy_json(revision=6)))
    disabled = api.disable(PROJECT_ID, expected_revision=5)
    assert disabled.revision == 6
    assert dict(disable_route.calls.last.request.url.params) == {
        "expected_revision": "5"
    }

    triggers = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/learning-policy/triggers"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "items": [
                    {
                        "id": REQUEST_ID,
                        "policy_revision": 5,
                        "dataset_id": PARENT_ID,
                        "status": "completed",
                        "candidate_digest": "b" * 64,
                        "train_digest": "c" * 64,
                        "validation_digest": "d" * 64,
                        "changed_samples": 21,
                        "message": None,
                        "created_at": "2026-10-16T00:00:00Z",
                        "completed_at": None,
                        "run_id": None,
                    }
                ],
            },
        )
    )
    trigger_page = api.triggers(PROJECT_ID, limit=5, offset=10)
    assert trigger_page.items[0].changed_samples == 21
    assert triggers.calls.last.request.url.params["offset"] == "10"

    delete_route = respx_mock.delete(
        f"{API}/projects/{PROJECT_ID}/learning-policy"
    ).mock(return_value=httpx.Response(204))
    assert api.delete(PROJECT_ID) is None
    assert delete_route.called


def test_policy_revision_conflict_is_server_authoritative(client, respx_mock):
    route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/learning-policy/disable"
    ).mock(
        return_value=httpx.Response(
            409,
            json={"detail": {"code": "stale_revision", "message": "refresh"}},
        )
    )
    api = LearningPolicies(client._http)
    with pytest.raises(ConflictError) as error:
        api.disable(PROJECT_ID, expected_revision=4)
    assert error.value.detail == {"code": "stale_revision", "message": "refresh"}
    assert route.calls.last.request.url.params["expected_revision"] == "4"
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (409, ConflictError),
        (422, ValidationError),
    ],
)
def test_policy_write_errors_preserve_server_detail(
    client, respx_mock, status, error_type
):
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/learning-policy/check").mock(
        return_value=httpx.Response(status, json={"detail": {"code": "denied"}})
    )
    with pytest.raises(error_type) as error:
        LearningPolicies(client._http).check(PROJECT_ID)
    assert error.value.detail == {"code": "denied"}
    assert route.call_count == 1
