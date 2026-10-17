from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest

from ai_annotation.errors import (
    AuthenticationError,
    ConflictError,
    PermissionDeniedError,
    ValidationError,
)
from ai_annotation.model_management import SampleEvaluations
from ai_annotation.model_management_models import SampleEvaluationCreateRequest

from .conftest import API

PROJECT_ID = "00000000-0000-0000-0000-000000000021"
MODEL_ID = "00000000-0000-0000-0000-000000000022"
DEPLOYMENT_ID = "00000000-0000-0000-0000-000000000023"
DATASET_ID = "00000000-0000-0000-0000-000000000024"
EVALUATION_ID = "00000000-0000-0000-0000-000000000025"
ATTEMPT_ID = "00000000-0000-0000-0000-000000000026"
TASK_ID = "00000000-0000-0000-0000-000000000027"
REQUEST_ID = "00000000-0000-0000-0000-000000000028"
SHA256 = "a" * 64


def profile_json():
    return {
        "schema": 1,
        "implementation": "aap-ultralytics-detection",
        "revision": 1,
        "ultralytics_version": "8.4.177",
        "torch_version": "2.8.0",
        "pillow_version": "11.0.0",
        "source_digest": "b" * 64,
        "storage_identity": DATASET_ID,
        "effective_device": "cpu",
        "options": {"deterministic": True},
        "profile_digest": "c" * 64,
    }


def frozen_sample_json():
    return {
        "ordinal": 0,
        "task_id": TASK_ID,
        "sha256": "d" * 64,
        "label_sha256": "e" * 64,
        "width": 640,
        "height": 480,
        "size_bytes": 1024,
        "split": "val",
        "explicit_negative": False,
        "truth": [],
    }


def spec_json():
    return {
        "schema": 1,
        "evaluation_id": EVALUATION_ID,
        "model_ref": {
            "model_version_id": MODEL_ID,
            "deployment_id": DEPLOYMENT_ID,
            "reference_digest": "f" * 64,
        },
        "dataset_id": DATASET_ID,
        "snapshot_digest": "1" * 64,
        "manifest_digest": "2" * 64,
        "package_sha256": "3" * 64,
        "benchmark_digest": "4" * 64,
        "comparison_digest": "5" * 64,
        "classes": ["cat"],
        "native_to_benchmark": [0],
        "role": "validation_selection",
        "held_out_confirmed": False,
        "confidence": 0.25,
        "matching_iou": 0.5,
        "nms_iou": 0.7,
        "evaluator": {"name": "fixed-detector"},
        "preprocessing": {"image_mode": "RGB"},
        "profile": profile_json(),
        "profile_digest": "c" * 64,
        "samples": [frozen_sample_json()],
        "issued_at": 1792108800,
        "expires_at": 1792195200,
        "opaque_frozen_evidence": {"source_version_id": DATASET_ID},
    }


def evaluation_json(status="queued", attempt_status="queued", attempt_id=ATTEMPT_ID):
    return {
        "id": EVALUATION_ID,
        "project_id": PROJECT_ID,
        "name": "candidate smoke",
        "model_version_id": MODEL_ID,
        "deployment_id": DEPLOYMENT_ID,
        "dataset_id": DATASET_ID,
        "request_id": REQUEST_ID,
        "status": status,
        "spec_digest": SHA256,
        "benchmark_digest": "4" * 64,
        "comparison_digest": "5" * 64,
        "spec": spec_json(),
        "current_attempt_id": attempt_id,
        "async_job_id": REQUEST_ID,
        "progress": {"pending": 1, "inflight": 0, "succeeded": 0, "failed": 0},
        "error_code": "cancel_requested" if status == "cancelling" else None,
        "error_message": None,
        "attempts": [
            {
                "id": attempt_id,
                "ordinal": 1,
                "generation": 1,
                "status": attempt_status,
                "heartbeat_at": None,
                "started_at": None,
                "completed_at": None,
                "error_code": None,
                "error_message": None,
                "report": None,
            }
        ],
        "created_at": "2026-10-16T00:00:00Z",
        "completed_at": None,
        "deletion_requested_at": None,
    }


def sample_result_json():
    return {
        **frozen_sample_json(),
        "status": "failed",
        "predictions": [],
        "matching": None,
        "derived_sha256": None,
        "preprocessing": None,
        "request_id": REQUEST_ID,
        "binding_digest": None,
        "inference": None,
        "produced_attempt_id": None,
        "error_code": "backend_unavailable",
        "error_message": "worker unavailable",
        "evidence_digest": None,
    }


def test_sample_evaluation_operations_match_current_responses(
    client, respx_mock, tmp_path
):
    api = SampleEvaluations(client._http)
    qualification = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/sample-evaluations/qualification"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "model_ref": {"model_version_id": MODEL_ID},
                "profile": profile_json(),
                "evaluator": {"implementation": "fixed"},
                "preprocessing": {"mode": "RGB"},
                "limits": {"samples": 32, "pixels": 40000000},
            },
        )
    )
    qualified = api.qualification(PROJECT_ID, MODEL_ID, DEPLOYMENT_ID)
    assert qualified.profile.profile_digest == "c" * 64
    assert dict(qualification.calls.last.request.url.params) == {
        "model_version_id": MODEL_ID,
        "deployment_id": DEPLOYMENT_ID,
    }

    create = respx_mock.post(f"{API}/projects/{PROJECT_ID}/sample-evaluations").mock(
        return_value=httpx.Response(202, json=evaluation_json())
    )
    created = api.create(
        PROJECT_ID,
        SampleEvaluationCreateRequest(
            request_id=REQUEST_ID,
            name=" candidate smoke ",
            model_version_id=MODEL_ID,
            deployment_id=DEPLOYMENT_ID,
            dataset_id=DATASET_ID,
            sample_task_ids=[TASK_ID],
        ),
    )
    body = json.loads(create.calls.last.request.content)
    assert body == {
        "request_id": REQUEST_ID,
        "name": "candidate smoke",
        "model_version_id": MODEL_ID,
        "deployment_id": DEPLOYMENT_ID,
        "dataset_id": DATASET_ID,
        "sample_task_ids": [TASK_ID],
    }
    assert created.status == "queued"
    assert created.spec.model_ref["reference_digest"] == "f" * 64
    assert created.spec.model_extra["opaque_frozen_evidence"] == {
        "source_version_id": DATASET_ID
    }

    listed = respx_mock.get(f"{API}/projects/{PROJECT_ID}/sample-evaluations").mock(
        return_value=httpx.Response(
            200, json={"items": [evaluation_json()], "total": 1}
        )
    )
    page = api.list(PROJECT_ID, limit=8, offset=16)
    assert page.items[0].id == UUID(EVALUATION_ID)
    assert page.total == 1
    assert listed.calls.last.request.url.params["offset"] == "16"

    detail_route = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/sample-evaluations/{EVALUATION_ID}"
    ).mock(return_value=httpx.Response(200, json=evaluation_json()))
    assert api.get(PROJECT_ID, EVALUATION_ID).id == UUID(EVALUATION_ID)
    assert detail_route.called

    compare = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/sample-evaluations/compare"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "compatible": False,
                "reasons": ["different frozen dataset"],
                "candidates": [
                    {
                        "evaluation_id": EVALUATION_ID,
                        "name": "candidate smoke",
                        "status": "queued",
                        "attempt_id": ATTEMPT_ID,
                        "model_ref": {"model_version_id": MODEL_ID},
                        "spec_digest": SHA256,
                        "comparison_digest": "5" * 64,
                        "aggregate": None,
                    },
                    {
                        "evaluation_id": REQUEST_ID,
                        "name": "baseline",
                        "status": "failed",
                        "attempt_id": ATTEMPT_ID,
                        "model_ref": {"model_version_id": MODEL_ID},
                        "spec_digest": "6" * 64,
                        "comparison_digest": "7" * 64,
                        "aggregate": None,
                    },
                ],
                "delta_right_minus_left": None,
                "sample_pairs": [],
            },
        )
    )
    comparison = api.compare(PROJECT_ID, EVALUATION_ID, REQUEST_ID)
    assert comparison.compatible is False
    assert dict(compare.calls.last.request.url.params) == {
        "left_id": EVALUATION_ID,
        "right_id": REQUEST_ID,
    }

    report_route = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/sample-evaluations/{EVALUATION_ID}/attempts/{ATTEMPT_ID}/report"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "schema": 1,
                "evaluation_id": EVALUATION_ID,
                "attempt_id": ATTEMPT_ID,
                "spec_digest": SHA256,
                "benchmark_digest": "4" * 64,
                "comparison_digest": "5" * 64,
                "model_ref": {"model_version_id": MODEL_ID},
                "evaluator": {"implementation": "fixed"},
                "status": "failed",
                "complete": False,
                "total_samples": 1,
                "succeeded_samples": 0,
                "failed_samples": 1,
                "pending_samples": 0,
                "aggregate": None,
                "samples": [sample_result_json()],
            },
        )
    )
    report = api.report(PROJECT_ID, EVALUATION_ID, ATTEMPT_ID)
    assert report.status == "failed" and report.complete is False
    assert report.samples[0].error_code == "backend_unavailable"
    assert report_route.called

    sample_page_route = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/sample-evaluations/{EVALUATION_ID}/attempts/{ATTEMPT_ID}/samples"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "evaluation_id": EVALUATION_ID,
                "attempt_id": ATTEMPT_ID,
                "report_sha256": "8" * 64,
                "spec_digest": SHA256,
                "total": 1,
                "offset": 0,
                "limit": 12,
                "items": [sample_result_json()],
            },
        )
    )
    sample_page = api.samples(
        PROJECT_ID,
        EVALUATION_ID,
        ATTEMPT_ID,
        class_index=0,
        confidence_min=0.0,
        offset=0,
        limit=12,
    )
    assert sample_page.items[0].task_id == UUID(TASK_ID)
    assert sample_page_route.calls.last.request.url.params["class_index"] == "0"
    assert sample_page_route.calls.last.request.url.params["confidence_min"] == "0.0"

    source_route = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/sample-evaluations/{EVALUATION_ID}/attempts/{ATTEMPT_ID}/samples/0/source"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "evaluation_id": EVALUATION_ID,
                "attempt_id": ATTEMPT_ID,
                "ordinal": 0,
                "checked_at": "2026-10-16T00:00:00Z",
                "source": "found",
                "task_id": TASK_ID,
                "project_id": PROJECT_ID,
                "batch_id": None,
                "media": "same",
                "labels": "changed",
                "current_sha256": "d" * 64,
                "current_label_sha256": "e" * 64,
                "current_width": 640,
                "current_height": 480,
                "task_status": "completed",
                "reviewed": True,
                "negative_confirmed": False,
                "reason": None,
            },
        )
    )
    source = api.source(PROJECT_ID, EVALUATION_ID, ATTEMPT_ID, 0)
    assert source.media == "same" and source.labels == "changed"
    assert source_route.called

    image_route = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/sample-evaluations/{EVALUATION_ID}/attempts/{ATTEMPT_ID}/samples/0/image"
    ).mock(return_value=httpx.Response(200, content=b"\x89PNG\r\n\x1a\nimage"))
    output = tmp_path / "frozen.png"
    assert (
        api.download_image(PROJECT_ID, EVALUATION_ID, ATTEMPT_ID, 0, output) == output
    )
    assert output.read_bytes() == b"\x89PNG\r\n\x1a\nimage"
    assert image_route.calls.last.request.headers["Authorization"] == "Bearer ak_test"

    cancel_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/sample-evaluations/{EVALUATION_ID}/cancel"
    ).mock(
        return_value=httpx.Response(
            200, json=evaluation_json(status="cancelling", attempt_status="cancelling")
        )
    )
    assert api.cancel(PROJECT_ID, EVALUATION_ID).status == "cancelling"
    assert cancel_route.call_count == 1

    retry_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/sample-evaluations/{EVALUATION_ID}/retry"
    ).mock(
        return_value=httpx.Response(
            202,
            json=evaluation_json(
                status="queued", attempt_status="queued", attempt_id=REQUEST_ID
            ),
        )
    )
    retried = api.retry(PROJECT_ID, EVALUATION_ID)
    assert retried.current_attempt_id == UUID(REQUEST_ID)
    assert retry_route.call_count == 1

    delete_route = respx_mock.delete(
        f"{API}/projects/{PROJECT_ID}/sample-evaluations/{EVALUATION_ID}"
    ).mock(return_value=httpx.Response(204))
    assert api.delete(PROJECT_ID, EVALUATION_ID) is None
    assert delete_route.call_count == 1


def test_sample_evaluation_create_preserves_frozen_request_identity():
    request = SampleEvaluationCreateRequest(
        request_id=REQUEST_ID,
        name="evaluation",
        model_version_id=MODEL_ID,
        deployment_id=DEPLOYMENT_ID,
        dataset_id=DATASET_ID,
        sample_task_ids=[TASK_ID],
        role="held_out_test",
        held_out_confirmed=True,
        confidence=0.0,
        matching_iou=0.6,
        nms_iou=0.8,
    )
    assert (
        request.model_dump(mode="json", exclude_unset=True)["dataset_id"] == DATASET_ID
    )
    assert request.model_dump(mode="json", exclude_unset=True)["confidence"] == 0.0
    with pytest.raises(ValueError, match="must not contain duplicates"):
        SampleEvaluationCreateRequest(
            request_id=REQUEST_ID,
            name="evaluation",
            model_version_id=MODEL_ID,
            deployment_id=DEPLOYMENT_ID,
            dataset_id=DATASET_ID,
            sample_task_ids=[TASK_ID, TASK_ID],
        )
    with pytest.raises(ValueError, match="held_out_confirmed"):
        SampleEvaluationCreateRequest(
            request_id=REQUEST_ID,
            name="evaluation",
            model_version_id=MODEL_ID,
            deployment_id=DEPLOYMENT_ID,
            dataset_id=DATASET_ID,
            sample_task_ids=[TASK_ID],
            role="held_out_test",
        )


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (409, ConflictError),
        (422, ValidationError),
    ],
)
def test_sample_evaluation_mutation_errors_keep_detail_without_retry(
    client, respx_mock, status, error_type
):
    route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/sample-evaluations/{EVALUATION_ID}/retry"
    ).mock(
        return_value=httpx.Response(status, json={"detail": {"code": "retry_denied"}})
    )
    with pytest.raises(error_type) as error:
        SampleEvaluations(client._http).retry(PROJECT_ID, EVALUATION_ID)
    assert error.value.detail == {"code": "retry_denied"}
    assert route.call_count == 1
