from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from aap_protocol_v2 import (
    CocoRlePayload,
    NativeMaskCandidate,
    NativeMaskCandidateValue,
    encode_low_res_mask,
    native_mask_candidate_id,
)

from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.task import Task
from aap_inference_contract.backend_control import BackendControlSetupResult
from aap_inference_contract.execution import (
    ExecutionProvenance,
    ModelExecutionObservation,
    ModelOperationResult,
    ModelTaskResult,
    thaw_json,
)
from app.services.ai_mask_session import verify_ai_mask_session
from app.services.ai_mask_receipt import verify_ai_mask_receipt
from tests.conftest import create_registry_with_pool


async def _seed(db, owner_id):
    suffix = uuid.uuid4().hex[:8]
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-NM-{suffix}",
        name=f"native-mask-{suffix}",
        type_label="image-seg",
        type_key="image-seg",
        owner_id=owner_id,
    )
    db.add(project)
    await db.flush()
    backend, pool = await create_registry_with_pool(
        db,
        name=f"native-mask-{suffix}",
        url=f"http://native-mask-{suffix}/",
        is_interactive=True,
        state="connected",
        enabled_pool=True,
    )
    db.add(ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True))
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"T-NM-{suffix}",
        file_name="image.png",
        file_path="http://example/image.png",
        status="pending",
    )
    db.add(task)
    await db.flush()
    return project, backend, task, pool


def _setup(outputs: list[str]) -> dict:
    return {
        "name": "test-native-mask",
        "infra": "pytorch",
        "models": [
            {
                "id": "native-interactive",
                "task": "interactive_seg",
                "is_interactive": True,
                "supported_prompts": ["point", "interactive_box", "mask", "scribble"],
                "supported_inputs": [
                    "full_image",
                    "point_prompt",
                    "bbox_prompt",
                    "mask_prompt",
                    "scribble_prompt",
                ],
                "supported_geometric_outputs": outputs,
            },
            {
                "id": "mask-tracker",
                "task": "tracker",
                "is_interactive": True,
                "supported_prompts": ["bbox"],
                "supported_inputs": ["video"],
                "supported_geometric_outputs": ["bbox", "polygon", "mask"],
            },
        ],
    }


def _candidate(prompt_revision: str) -> dict:
    rle = CocoRlePayload(
        encoding="coco_rle",
        size=[2, 3],
        counts=[1, 2, 2, 1],
    )
    return NativeMaskCandidate(
        value=NativeMaskCandidateValue(rle=rle, masklabels=["object"]),
        score=0.91,
        candidate_id=native_mask_candidate_id(
            rle,
            prompt_revision=prompt_revision,
            candidate_index=0,
        ),
    ).model_dump(mode="json")


def _url(project, backend) -> str:
    return (
        f"/api/v1/projects/{project.id}/ml-backends/{backend.id}/interactive-annotating"
    )


def _patch_setup(monkeypatch, setup):
    async def setup_backend(registry_id):
        return BackendControlSetupResult(registry_id=registry_id, setup=setup)

    monkeypatch.setattr(
        "app.api.v1.ml_backends.backend_control_service",
        lambda _db: SimpleNamespace(setup=setup_backend),
    )


def _patch_execution(monkeypatch, captured, *, pool_id, respond):
    class FakeExecutionPort:
        async def execute(self, operation):
            captured["model_called"] = True
            task_data = thaw_json(operation.task_inputs[0])
            context = thaw_json(operation.context) or {}
            captured["operation"] = operation
            captured["task_data"] = task_data
            captured["context"] = context
            if respond is None:
                raise AssertionError("native output guard allowed model execution")
            result = respond(operation, task_data, context)
            registry_id = operation.requested_registry_id
            return ModelOperationResult(
                request_id=operation.request_id,
                operation=operation.operation,
                results=(result,),
                provenance=ExecutionProvenance(
                    requested_registry_id=operation.requested_registry_id,
                    requested_pool_id=operation.requested_pool_id,
                    registry_id=registry_id,
                    pool_id=pool_id,
                    instance_id=registry_id,
                    route_scope_id=uuid.uuid4(),
                    attempt_id=operation.request_id,
                    topology_generation=operation.topology_generation,
                    member_id=operation.selected_member_id,
                ),
                observation=ModelExecutionObservation(
                    backend_id=str(registry_id),
                    operation="predict_interactive",
                    outcome="success",
                    elapsed_seconds=0.001,
                ),
                wall_time_ms=1,
            )

    monkeypatch.setattr(
        "app.api.v1.ml_backends.model_execution_port",
        lambda *_args, **_kwargs: FakeExecutionPort(),
    )


async def test_native_endpoint_rejects_when_only_tracker_supports_mask(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    project, backend, task, pool = await _seed(db_session, user.id)
    await db_session.commit()

    captured = {}
    _patch_setup(monkeypatch, _setup(["polygon"]))
    _patch_execution(
        monkeypatch,
        captured,
        pool_id=pool.id,
        respond=None,
    )
    response = await httpx_client.post(
        _url(project, backend),
        json={
            "task_id": str(task.id),
            "context": {
                "type": "point",
                "points": [[0.5, 0.5]],
                "labels": [1],
                "output_geometry": "mask",
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "unsupported_output_geometry"
    assert not captured.get("model_called")


async def test_mask_prompt_source_requires_annotations_read_scope(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    project, backend, task, _ = await _seed(db_session, user.id)
    await db_session.commit()
    created = await httpx_client.post(
        "/api/v1/me/api-keys",
        json={"name": "mask-scope-test", "scopes": ["datasets:read"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert created.status_code == 201, created.text
    api_key = created.json()["plaintext"]
    resolver = AsyncMock()
    monkeypatch.setattr(
        "app.api.v1.ml_backends.resolve_authorized_mask_prompt",
        resolver,
    )

    response = await httpx_client.post(
        _url(project, backend),
        json={
            "task_id": str(task.id),
            "context": {
                "type": "point",
                "points": [[0.5, 0.5]],
                "mask_prompt_source": {
                    "annotation_id": str(uuid.uuid4()),
                    "source_version": 1,
                },
            },
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert response.status_code == 403
    assert "annotations:read" in response.json()["detail"]
    resolver.assert_not_awaited()


@pytest.mark.parametrize(
    ("backend_result", "diagnostic", "expected_status", "expected_reason"),
    [
        (
            [{"type": "mask", "value": {"rle": {}}, "candidate_id": "bad"}],
            None,
            502,
            "invalid_mask_payload",
        ),
        (
            [
                {
                    "type": "mask",
                    "value": {
                        "rle": {
                            "encoding": "coco_rle",
                            "size": [1, 4097],
                            "counts": [4097],
                        },
                        "masklabels": ["object"],
                    },
                    "score": 0.5,
                    "candidate_id": "sha256:" + "0" * 64,
                }
            ],
            None,
            413,
            "mask_payload_too_large",
        ),
        ([], {"reason": "empty_mask", "retryable": False}, 200, None),
    ],
)
async def test_native_endpoint_normalizes_error_and_empty_contracts(
    httpx_client,
    super_admin,
    db_session,
    monkeypatch,
    backend_result,
    diagnostic,
    expected_status,
    expected_reason,
):
    user, token = super_admin
    project, backend, task, pool = await _seed(db_session, user.id)
    await db_session.commit()

    _patch_setup(monkeypatch, _setup(["polygon", "mask"]))

    def respond(operation, task_data, _context):
        assert operation.max_response_bytes == 16 * 1024 * 1024
        return ModelTaskResult(
            task_id=task_data["id"],
            result=backend_result,
            diagnostic=diagnostic,
            score=0.73,
            model_version="native-mask-model",
            inference_time_ms=17,
            cache_hit=False,
            model_load_ms=29,
            meta={"result_origin": "test"},
        )

    captured = {}
    _patch_execution(
        monkeypatch,
        captured,
        pool_id=pool.id,
        respond=respond,
    )
    response = await httpx_client.post(
        _url(project, backend),
        json={
            "task_id": str(task.id),
            "context": {
                "type": "point",
                "points": [[0.5, 0.5]],
                "labels": [1],
                "output_geometry": "mask",
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == expected_status, response.text
    if expected_reason is not None:
        assert response.json()["detail"]["reason"] == expected_reason
    else:
        assert response.json()["result"] == []
        assert response.json()["diagnostic"]["reason"] == "empty_mask"
        assert response.json()["score"] == pytest.approx(0.73)
        assert response.json()["model_version"] == "native-mask-model"
        assert response.json()["inference_time_ms"] == 17
        assert response.json()["cache_hit"] is False
        assert response.json()["model_load_ms"] == 29


async def test_native_endpoint_returns_valid_candidate_with_route_lineage(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    project, backend, task, pool = await _seed(db_session, user.id)
    await db_session.commit()

    raw_mask_input = encode_low_res_mask(np.zeros((256, 256), dtype=np.float32))
    _patch_setup(monkeypatch, _setup(["polygon", "mask"]))

    def respond(_operation, task_data, context):
        return ModelTaskResult(
            task_id=task_data["id"],
            result=(_candidate(context["prompt_revision"]),),
            score=0.86,
            model_version="test-model",
            inference_time_ms=19,
            cache_hit=True,
            model_load_ms=23,
            meta={"output": "native-mask"},
            mask_input_next=raw_mask_input,
        )

    captured = {}
    _patch_execution(
        monkeypatch,
        captured,
        pool_id=pool.id,
        respond=respond,
    )
    response = await httpx_client.post(
        _url(project, backend),
        json={
            "task_id": str(task.id),
            "context": {
                "type": "point",
                "points": [[0.5, 0.5]],
                "labels": [1],
                "output_geometry": "mask",
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["result"][0]["value"]["rle"]["size"] == [2, 3]
    assert payload["model_version"] == "test-model"
    assert payload["score"] == pytest.approx(0.86)
    assert payload["inference_time_ms"] == 19
    assert payload["cache_hit"] is True
    assert payload["model_load_ms"] == 23
    assert payload["routing"]["requested_backend_id"] == str(backend.id)
    assert payload["routing"]["backend_instance_id"] == str(backend.id)
    assert payload["routing"]["backend_pool_id"] == str(pool.id)
    assert payload["routing"]["model_id"] == "native-interactive"
    claims = verify_ai_mask_session(payload["mask_input_next"])
    assert claims["task_id"] == str(task.id)
    assert claims["frame_index"] is None
    assert claims["requested_backend_id"] == str(backend.id)
    assert claims["model_id"] == "native-interactive"
    assert claims["model_variants"] == {}
    receipt = verify_ai_mask_receipt(
        payload["accept_receipts"][payload["result"][0]["candidate_id"]]
    )
    assert receipt["prompt_summary"] == payload["prompt_summary"]
    assert receipt["prompt_summary"]["positive_points"] == 1
    assert receipt["accept_target"] == {
        "mode": "create",
        "source_annotation_id": None,
        "source_version": None,
        "frame_index": None,
    }
    assert receipt["prompt_source"] is None


async def test_native_endpoint_receipt_counts_positive_and_negative_scribbles(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    project, backend, task, pool = await _seed(db_session, user.id)
    await db_session.commit()

    _patch_setup(monkeypatch, _setup(["polygon", "mask"]))

    def respond(_operation, task_data, context):
        return ModelTaskResult(
            task_id=task_data["id"],
            result=(_candidate(context["prompt_revision"]),),
            score=0.86,
            model_version="test-model",
            inference_time_ms=19,
            cache_hit=True,
            model_load_ms=23,
            meta={"output": "native-mask"},
        )

    captured = {}
    _patch_execution(
        monkeypatch,
        captured,
        pool_id=pool.id,
        respond=respond,
    )
    response = await httpx_client.post(
        _url(project, backend),
        json={
            "task_id": str(task.id),
            "context": {
                "type": "scribble",
                "scribbles": [
                    {
                        "polarity": 1,
                        "points": [[0.1, 0.1], [0.4, 0.4]],
                        "width": 0.01,
                    },
                    {
                        "polarity": 0,
                        "points": [[0.6, 0.6], [0.8, 0.8]],
                        "width": 0.01,
                    },
                ],
                "output_geometry": "mask",
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    summary = payload["prompt_summary"]
    assert summary["family"] == "scribble"
    assert summary["positive_scribbles"] == 1
    assert summary["negative_scribbles"] == 1
    receipt = verify_ai_mask_receipt(
        payload["accept_receipts"][payload["result"][0]["candidate_id"]]
    )
    assert receipt["prompt_summary"] == summary
