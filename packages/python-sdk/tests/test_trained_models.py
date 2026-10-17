from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from ai_annotation.errors import (
    AuthenticationError,
    ConflictError,
    PermissionDeniedError,
    ValidationError,
)
from ai_annotation.model_management import ModelReleases, TrainedModels
from ai_annotation.model_management_models import (
    ModelDecisionCreateRequest,
    ModelDeploymentCreateRequest,
    ModelReleaseCreateRequest,
    TrainedModelCreateRequest,
)

from .conftest import API

PROJECT_ID = "00000000-0000-0000-0000-000000000001"
MODEL_ID = "00000000-0000-0000-0000-000000000002"
RUN_ID = "00000000-0000-0000-0000-000000000003"
ATTEMPT_ID = "00000000-0000-0000-0000-000000000004"
REGISTRY_ID = "00000000-0000-0000-0000-000000000005"
RELEASE_ID = "00000000-0000-0000-0000-000000000006"
REQUEST_ID = "00000000-0000-0000-0000-000000000007"
SHA256 = "a" * 64


def model_json(**updates):
    return {
        "id": MODEL_ID,
        "project_id": PROJECT_ID,
        "run_id": RUN_ID,
        "attempt_id": ATTEMPT_ID,
        "name": "candidate",
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "artifact": {"sha256": SHA256, "size_bytes": 123, "key": "weights"},
        "classes": ["cat"],
        "bindings": ["cat"],
        "binding_digest": SHA256,
        "provenance": {"task": "detect", "dataset_id": RUN_ID},
        "evaluation": {},
        "created_at": "2026-10-16T00:00:00Z",
        "task": "detect",
        "media_type": "image",
        "disposition": None,
        "decision_sequence": 0,
        "archived": False,
        "is_current_release": False,
        "future_field": {"preserved": True},
        **updates,
    }


def deployment_json():
    return {
        "id": REGISTRY_ID,
        "model_version_id": MODEL_ID,
        "registry_id": REGISTRY_ID,
        "status": "ready",
        "receipt": {"loaded": True},
        "trial": {"sample_scope": "one_fixed_validation_image"},
        "error_message": None,
        "checked_at": "2026-10-16T00:00:00Z",
        "created_at": "2026-10-15T00:00:00Z",
    }


def test_trained_model_operations_match_current_responses(client, respx_mock):
    api = TrainedModels(client._http)
    create = respx_mock.post(f"{API}/projects/{PROJECT_ID}/trained-models").mock(
        return_value=httpx.Response(201, json=model_json())
    )
    result = api.create(
        PROJECT_ID,
        TrainedModelCreateRequest(
            request_id=REQUEST_ID,
            name="candidate",
            run_id=RUN_ID,
            attempt_id=ATTEMPT_ID,
            bindings=["cat"],
        ),
    )
    assert result.id == UUID(MODEL_ID)
    assert result.model_extra["future_field"] == {"preserved": True}
    assert json.loads(create.calls.last.request.content) == {
        "request_id": REQUEST_ID,
        "name": "candidate",
        "run_id": RUN_ID,
        "attempt_id": ATTEMPT_ID,
        "bindings": ["cat"],
    }

    listed = respx_mock.get(f"{API}/projects/{PROJECT_ID}/trained-models").mock(
        return_value=httpx.Response(
            200,
            json={"items": [model_json()], "total": 31},
        )
    )
    page = api.list(PROJECT_ID, limit=7, offset=14, archive="include")
    assert page.total == 31 and page.items[0].id == UUID(MODEL_ID)
    assert dict(listed.calls.last.request.url.params) == {
        "limit": "7",
        "offset": "14",
        "archive": "include",
    }

    detail_route = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/trained-models/{MODEL_ID}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"model": model_json(), "deployments": [deployment_json()]},
        )
    )
    detail = api.get(PROJECT_ID, MODEL_ID)
    assert detail.model.id == UUID(MODEL_ID)
    assert detail.deployments[0].status == "ready"
    assert detail_route.called

    descriptor_route = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/trained-models/{MODEL_ID}/download"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "url": "https://storage.invalid/weights",
                "sha256": SHA256,
                "size_bytes": 123,
            },
        )
    )
    streamed = {}

    def stream(url, dest, **kwargs):
        streamed.update(url=url, dest=dest, **kwargs)
        return dest

    client._http.stream_download = stream
    destination = Path("model.pt")
    assert api.download(PROJECT_ID, MODEL_ID, destination) == destination
    assert descriptor_route.called
    assert streamed == {
        "url": "https://storage.invalid/weights",
        "dest": destination,
        "expected_sha256": SHA256,
        "expected_size": 123,
    }

    deploy_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/trained-models/{MODEL_ID}/deploy"
    ).mock(return_value=httpx.Response(200, json=deployment_json()))
    deployed = api.deploy(
        PROJECT_ID, MODEL_ID, ModelDeploymentCreateRequest(registry_id=REGISTRY_ID)
    )
    assert deployed.status == "ready"
    assert json.loads(deploy_route.calls.last.request.content) == {
        "registry_id": REGISTRY_ID
    }

    lineage_route = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/model-lineage/model/{MODEL_ID}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "node": {
                    "kind": "model",
                    "id": MODEL_ID,
                    "name": "candidate",
                    "status": None,
                    "created_at": "2026-10-16T00:00:00Z",
                    "facts": {"weights_sha256": SHA256},
                },
                "inputs": [
                    {
                        "relation": "training_data",
                        "target": None,
                        "gap": "source unavailable",
                    }
                ],
                "sections": ["deployments", "releases", "evaluations"],
                "relation": "deployments",
                "items": [],
                "next_cursor": "opaque-cursor",
            },
        )
    )
    lineage = api.lineage(
        PROJECT_ID,
        "model",
        MODEL_ID,
        relation="deployments",
        cursor="cursor/with?reserved",
        limit=20,
    )
    assert lineage.node.id == UUID(MODEL_ID)
    assert lineage.next_cursor == "opaque-cursor"
    assert (
        lineage_route.calls.last.request.url.params["cursor"] == "cursor/with?reserved"
    )

    decisions_route = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/trained-models/{MODEL_ID}/decisions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"items": [], "next_before": 4, "server_extension": True},
        )
    )
    decision_page = api.decisions(PROJECT_ID, MODEL_ID, before=5, limit=4)
    assert decision_page.next_before == 4
    assert decisions_route.calls.last.request.url.params["before"] == "5"

    decision_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/trained-models/{MODEL_ID}/decisions"
    ).mock(
        return_value=httpx.Response(
            201,
            json={
                "id": REQUEST_ID,
                "project_id": PROJECT_ID,
                "model_version_id": MODEL_ID,
                "sequence": 1,
                "creator_id": None,
                "creator_label": "Manager",
                "disposition": "research",
                "note": "keep for comparison",
                "created_at": "2026-10-16T00:00:00Z",
            },
        )
    )
    decision = api.decide(
        PROJECT_ID,
        MODEL_ID,
        ModelDecisionCreateRequest(
            request_id=REQUEST_ID,
            disposition="research",
            note="keep for comparison",
        ),
    )
    assert decision.sequence == 1
    assert decision_route.call_count == 1


def test_model_release_cas_and_explicit_nulls(client, respx_mock):
    api = ModelReleases(client._http)
    route = respx_mock.get(f"{API}/projects/{PROJECT_ID}/model-releases").mock(
        return_value=httpx.Response(
            200,
            json={
                "current": None,
                "generation": 8,
                "current_release_id": None,
                "total": 2,
                "items": [],
            },
        )
    )
    page = api.list(PROJECT_ID, limit=10, offset=20)
    assert page.current is None and page.generation == 8
    assert route.calls.last.request.url.params["offset"] == "20"

    create = respx_mock.post(f"{API}/projects/{PROJECT_ID}/model-releases").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": RELEASE_ID,
                "project_id": PROJECT_ID,
                "model_version_id": None,
                "deployment_id": None,
                "previous_release_id": None,
                "rollback_from_id": RELEASE_ID,
                "generation": 9,
                "snapshot": {"action": "rollback"},
                "created_at": "2026-10-16T00:00:00Z",
            },
        )
    )
    release = api.create(
        PROJECT_ID,
        ModelReleaseCreateRequest(
            request_id=REQUEST_ID,
            expected_generation=8,
            model_version_id=None,
            deployment_id=None,
            rollback_from_id=RELEASE_ID,
        ),
    )
    assert release.generation == 9
    assert json.loads(create.calls.last.request.content) == {
        "request_id": REQUEST_ID,
        "expected_generation": 8,
        "model_version_id": None,
        "deployment_id": None,
        "rollback_from_id": RELEASE_ID,
    }


def test_model_release_rejects_stale_generation_without_retry(client, respx_mock):
    stale = respx_mock.post(f"{API}/projects/{PROJECT_ID}/model-releases").mock(
        return_value=httpx.Response(409, json={"detail": {"code": "stale_generation"}})
    )
    api = ModelReleases(client._http)
    with pytest.raises(ConflictError) as error:
        api.create(
            PROJECT_ID,
            ModelReleaseCreateRequest(request_id=REQUEST_ID, expected_generation=8),
        )
    assert error.value.detail == {"code": "stale_generation"}
    assert stale.call_count == 1


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (409, ConflictError),
        (422, ValidationError),
    ],
)
def test_trained_model_errors_keep_server_details_without_write_retry(
    client, respx_mock, status, error_type
):
    route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/trained-models/{MODEL_ID}/decisions"
    ).mock(
        return_value=httpx.Response(status, json={"detail": {"code": "server_code"}})
    )
    api = TrainedModels(client._http)
    with pytest.raises(error_type) as error:
        api.decide(
            PROJECT_ID,
            MODEL_ID,
            ModelDecisionCreateRequest(
                request_id=REQUEST_ID,
                disposition="active",
                note="accept",
            ),
        )
    assert error.value.detail == {"code": "server_code"}
    assert route.call_count == 1
