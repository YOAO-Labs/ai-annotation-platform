"""Mocked REST contracts for annotation transformations and candidate decisions."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ai_annotation.annotation_operation_models import (
    AiMaskAcceptRequest,
    AiMaskAcceptTarget,
    AiMaskCandidateInput,
    AiMaskInferenceLineage,
    AiMaskPromptSummary,
    AiMaskRoutingLineage,
    AnnotationConversionDryRunRequest,
    AnnotationConversionExecuteRequest,
    AnnotationSliceRestoreRequest,
    InterpolateRangeRequest,
    MaskCreateMutation,
    MaskDeleteMutation,
    MaskExpectedVersion,
    MaskMutationCommitRequest,
    MaskMutationScope,
    MaskUpdateMutation,
    NativeMaskCandidate,
    NativeMaskCandidateValue,
    NativeMaskRle,
    PolygonSliceCommitRequest,
    PropagateBatchRequest,
    PropagateRequest,
    RasterMaskGeometry,
    SecondaryInferenceRequest,
)
from ai_annotation.annotation_operations import AnnotationOperations
from ai_annotation.cli import annotation_operations as operations_cli
from ai_annotation.errors import APIStatusError
from ai_annotation.video_review_models import CocoRleMaskRef

from .conftest import API

runner = CliRunner()
TASK_ID = UUID("00000000-0000-4000-8000-000000000101")
TARGET_TASK_ID = UUID("00000000-0000-4000-8000-000000000102")
ANNOTATION_ID = UUID("00000000-0000-4000-8000-000000000201")
ANNOTATION_ID_2 = UUID("00000000-0000-4000-8000-000000000202")
PREDICTION_ID = UUID("00000000-0000-4000-8000-000000000301")
SEGMENT_ID = UUID("00000000-0000-4000-8000-000000000401")
USER_ID = UUID("00000000-0000-4000-8000-000000000501")
BACKEND_ID = UUID("00000000-0000-4000-8000-000000000601")
NOW = "2026-10-17T08:30:00Z"


def _annotation(*, annotation_id=ANNOTATION_ID, task_id=TASK_ID, version=3) -> dict:
    return {
        "id": str(annotation_id),
        "task_id": str(task_id),
        "project_id": str(uuid4()),
        "source": "manual",
        "annotation_type": "bbox",
        "tool_unit_id": "bbox",
        "class_name": "car",
        "geometry": {"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        "version": version,
        "is_active": True,
        "created_at": NOW,
    }


def _conversion_summary() -> dict:
    return {
        "source_count": 1,
        "result_count": 1,
        "materialized_held_frames": 0,
        "lossy_count": 0,
    }


def _conversion_plan() -> dict:
    return {
        "plan_token": "cvp_" + "p" * 48,
        "expires_at": "2026-10-17T08:40:00Z",
        "target": "mask",
        "operation": "replace",
        "scope": "image",
        "items": [
            {
                "source_annotation_id": str(ANNOTATION_ID),
                "source_type": "polygon",
                "target_type": "raster_mask",
                "source_version": 3,
                "frame_indexes": [],
                "result_count": 1,
                "source_area_pixels": 4,
                "target_area_pixels": 4,
                "changed_pixels": 0,
                "source_components": 1,
                "target_components": 1,
                "source_holes": 0,
                "target_holes": 0,
                "source_vertices": 4,
                "target_vertices": 4,
                "materialized_held_frames": 0,
                "lossy": False,
                "reasons": [],
            }
        ],
        "summary": _conversion_summary(),
    }


def _conversion_result() -> dict:
    return {
        "operation_id": str(uuid4()),
        "updated_annotations": [_annotation(version=4)],
        "created_annotations": [],
        "deleted_annotation_ids": [],
        "lineage_edges": [
            {
                "source_annotation_id": str(ANNOTATION_ID),
                "result_annotation_id": str(ANNOTATION_ID),
                "source_version": 3,
                "result_version": 4,
                "frame_index": None,
            }
        ],
        "report": _conversion_summary(),
        "idempotent_replay": False,
    }


def _slice_receipt(*, operation_id: UUID | None = None) -> dict:
    operation_id = operation_id or uuid4()
    return {
        "operation_id": str(operation_id),
        "slice_operation_id": str(operation_id),
        "source_annotation_id": str(ANNOTATION_ID),
        "created_annotation_id": str(ANNOTATION_ID_2),
        "result_versions": {str(ANNOTATION_ID): 4, str(ANNOTATION_ID_2): 1},
        "active_annotation_ids": [str(ANNOTATION_ID), str(ANNOTATION_ID_2)],
        "target": "after",
        "restore_expires_at": "2026-11-16T08:30:00Z",
        "idempotent_replay": False,
        "no_op": False,
    }


def _mask_reference() -> dict:
    digest = "a" * 64
    return {
        "encoding": "coco_rle_ref",
        "storage_encoding": "identity",
        "size": [4, 4],
        "object_key": f"raster-masks/sha256/{digest[:2]}/{digest[2:4]}/{digest}.json",
        "sha256": digest,
        "runs": 4,
        "bytes": 64,
    }


def _prediction(**overrides) -> dict:
    return {
        "id": str(PREDICTION_ID),
        "task_id": str(TASK_ID),
        "project_id": str(uuid4()),
        "ml_backend_id": str(BACKEND_ID),
        "model_version": "mask-v1",
        "score": 0.91,
        "source": "interactive_native_mask",
        "tool_unit_id": "region",
        "result": [
            {
                "type": "mask",
                "class_name": "car",
                "geometry": {"type": "raster_mask", "mask": _mask_reference()},
                "confidence": 0.91,
                "attributes": {},
                "shape_index": 2,
            }
        ],
        "cluster": None,
        "created_at": NOW,
        "inference_time_ms": 20,
        "total_cost": None,
        **overrides,
    }


def _ai_mask_accept_response() -> dict:
    return {
        "prediction": _prediction(),
        "annotation": _annotation(version=5),
        "source_version": 4,
        "result_version": 5,
        "content_digest": "b" * 64,
        "replayed": False,
    }


def _ai_mask_accept_request() -> AiMaskAcceptRequest:
    return AiMaskAcceptRequest(
        idempotency_key="native-mask-accept-0001",
        candidate=AiMaskCandidateInput(
            candidate=NativeMaskCandidate(
                value=NativeMaskCandidateValue(
                    rle=NativeMaskRle(size=(2, 2), counts=[0, 1, 3]),
                    masklabels=["car"],
                ),
                score=0.9,
                candidate_id="sha256:" + "f" * 64,
            ),
            candidate_index=2,
            prompt_revision="prompt-rev-7",
            receipt="native-mask-receipt-private-value",
        ),
        class_name="car",
        video_segment_id=SEGMENT_ID,
        target=AiMaskAcceptTarget(
            mode="refine",
            source_annotation_id=ANNOTATION_ID,
            source_version=4,
            frame_index=12,
        ),
        prompt_summary=AiMaskPromptSummary(
            family="point",
            positive_points=1,
            parameters_digest="1" * 64,
        ),
        routing=AiMaskRoutingLineage(
            requested_backend_id=BACKEND_ID,
            backend_pool_id=uuid4(),
            backend_instance_id=BACKEND_ID,
            model_id="sam-image",
        ),
        inference=AiMaskInferenceLineage(
            model_version="sam-3", inference_time_ms=20, cache_hit=False
        ),
    )


def test_conversion_dry_run_and_execute_keep_plan_token_and_idempotency_explicit(
    client, respx_mock
):
    operations = AnnotationOperations(client._http)
    dry_route = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/annotation-conversions:dry-run"
    ).mock(return_value=httpx.Response(200, json=_conversion_plan()))
    execute_route = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/annotation-conversions:execute"
    ).mock(return_value=httpx.Response(200, json=_conversion_result()))

    request = AnnotationConversionDryRunRequest(
        annotation_ids=[ANNOTATION_ID],
        target="mask",
        operation="replace",
        scope="image",
    )
    plan = operations.conversion_dry_run(TASK_ID, request)
    assert plan.plan_token.startswith("cvp_")
    assert plan.expires_at.isoformat().startswith("2026-10-17T08:40:00")
    assert plan.summary.lossy_count == 0
    assert plan.plan_token not in repr(plan)
    assert json.loads(dry_route.calls.last.request.content) == {
        "annotation_ids": [str(ANNOTATION_ID)],
        "target": "mask",
        "operation": "replace",
        "scope": "image",
    }

    execution = AnnotationConversionExecuteRequest(
        plan_token=plan.plan_token,
        idempotency_key="conversion:test-operation-001",
        confirm_replace=True,
        confirm_lossy=False,
    )
    result = operations.conversion_execute(TASK_ID, execution)
    assert result.idempotent_replay is False
    assert result.updated_annotations[0].version == 4
    assert json.loads(execute_route.calls.last.request.content) == {
        "plan_token": plan.plan_token,
        "idempotency_key": "conversion:test-operation-001",
        "confirm_replace": True,
        "confirm_lossy": False,
    }

    with pytest.raises(ValidationError):
        AnnotationConversionDryRunRequest(
            annotation_ids=[ANNOTATION_ID],
            target="mask",
            scope="image",
            frame_index=0,
        )


def test_propagation_interpolation_and_secondary_inference_keep_exact_inputs(
    client, respx_mock
):
    operations = AnnotationOperations(client._http)
    propagate = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/annotations/{ANNOTATION_ID}/propagate-to-task"
    ).mock(
        return_value=httpx.Response(
            201,
            json={
                "annotation": _annotation(task_id=TARGET_TASK_ID),
                "motion_compensated": True,
            },
        )
    )
    batch = respx_mock.post(f"{API}/tasks/{TASK_ID}/annotations/propagate-batch").mock(
        return_value=httpx.Response(
            201,
            json={
                "items": [
                    {
                        "source_annotation_id": str(ANNOTATION_ID),
                        "annotation": _annotation(task_id=TARGET_TASK_ID),
                    }
                ],
                "motion_compensated": False,
            },
        )
    )
    interpolate = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/annotations/interpolate-range"
    ).mock(
        return_value=httpx.Response(
            201,
            json={
                "annotations": [_annotation(task_id=TARGET_TASK_ID)],
                "motion_compensated": True,
                "skipped_frames": [3],
            },
        )
    )
    secondary = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/annotations/{ANNOTATION_ID}/secondary-inference"
    ).mock(
        return_value=httpx.Response(
            201,
            json={
                "annotation": _annotation(),
                "created_children": [_annotation(annotation_id=ANNOTATION_ID_2)],
            },
        )
    )

    propagated = operations.propagate_to_task(
        TASK_ID,
        ANNOTATION_ID,
        PropagateRequest(
            target_task_id=TARGET_TASK_ID,
            override_psr={"center": [1, 2, 3]},
        ),
    )
    assert propagated.motion_compensated is True
    assert json.loads(propagate.calls.last.request.content) == {
        "target_task_id": str(TARGET_TASK_ID),
        "override_psr": {"center": [1, 2, 3]},
    }

    batch_result = operations.propagate_batch(
        TASK_ID, PropagateBatchRequest(target_task_id=TARGET_TASK_ID)
    )
    assert batch_result.items[0].source_annotation_id == ANNOTATION_ID
    assert json.loads(batch.calls.last.request.content) == {
        "target_task_id": str(TARGET_TASK_ID)
    }

    interpolated = operations.interpolate_range(
        TASK_ID,
        InterpolateRangeRequest(track_id="trk_car_1", to_task_id=TARGET_TASK_ID),
    )
    assert interpolated.skipped_frames == [3]
    assert json.loads(interpolate.calls.last.request.content) == {
        "track_id": "trk_car_1",
        "to_task_id": str(TARGET_TASK_ID),
    }

    inference = operations.secondary_inference(
        TASK_ID,
        ANNOTATION_ID,
        SecondaryInferenceRequest(
            ml_backend_id=BACKEND_ID,
            write_target="attributes",
            write_keys=["material"],
            model_id="segmentation-v4",
            model_variants={"image_size": "1024", "quality": "high"},
            params={"threshold": 0.37, "nested": {"mode": "precise"}},
            task="segment_anything",
            pad=0.12,
        ),
    )
    assert len(inference.created_children) == 1
    sent = json.loads(secondary.calls.last.request.content)
    assert sent["model_variants"] == {"image_size": "1024", "quality": "high"}
    assert sent["params"] == {"threshold": 0.37, "nested": {"mode": "precise"}}


def test_mask_commit_preserves_raster_reference_scope_fingerprint_and_versions(
    client, respx_mock
):
    operations = AnnotationOperations(client._http)
    route = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/annotations/mask-mutations:commit"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "operation_id": str(uuid4()),
                "updated_annotations": [{"id": str(ANNOTATION_ID), "version": 4}],
                "created_annotations": [{"id": str(ANNOTATION_ID_2), "version": 1}],
                "deleted_annotation_ids": [],
                "result_versions": {str(ANNOTATION_ID): 4, str(ANNOTATION_ID_2): 1},
                "lineage_edges": [
                    {
                        "source_annotation_id": str(ANNOTATION_ID),
                        "result_annotation_id": str(ANNOTATION_ID_2),
                        "relation": "split",
                        "source_version": 3,
                        "result_version": 1,
                        "frame_index": None,
                    }
                ],
                "before_digest": "c" * 64,
                "after_digest": "d" * 64,
                "audit_id": 17,
                "idempotent_replay": False,
                "slice_restore": None,
            },
        )
    )
    reference = CocoRleMaskRef.model_validate(_mask_reference())
    request = MaskMutationCommitRequest(
        idempotency_key="mask-op-test-0001",
        operation="split_components",
        scope=MaskMutationScope(media="image", class_name="car"),
        scope_fingerprint="e" * 64,
        expected_versions=[MaskExpectedVersion(annotation_id=ANNOTATION_ID, version=3)],
        mutations=[
            MaskUpdateMutation(
                kind="update",
                annotation_id=ANNOTATION_ID,
                geometry=RasterMaskGeometry(mask=reference),
            ),
            MaskCreateMutation(
                kind="create",
                source_annotation_ids=[ANNOTATION_ID],
                geometry=RasterMaskGeometry(mask=reference),
            ),
        ],
    )
    result = operations.commit_mask_mutations(TASK_ID, request)
    assert result.result_versions[str(ANNOTATION_ID)] == 4
    sent = json.loads(route.calls.last.request.content)
    assert sent["scope_fingerprint"] == "e" * 64
    assert sent["expected_versions"] == [
        {"annotation_id": str(ANNOTATION_ID), "version": 3}
    ]
    assert (
        sent["mutations"][0]["geometry"]["mask"]["object_key"] == reference.object_key
    )
    assert "counts" not in sent["mutations"][0]["geometry"]["mask"]

    with pytest.raises(ValidationError, match="sorted"):
        MaskMutationCommitRequest(
            idempotency_key="mask-op-test-0002",
            operation="split_components",
            scope=MaskMutationScope(media="image", class_name="car"),
            scope_fingerprint="e" * 64,
            expected_versions=[
                MaskExpectedVersion(annotation_id=ANNOTATION_ID_2, version=1),
                MaskExpectedVersion(annotation_id=ANNOTATION_ID, version=3),
            ],
            mutations=[MaskDeleteMutation(kind="delete", annotation_id=ANNOTATION_ID)],
        )


def test_mask_commit_conflict_returns_server_scope_without_auto_refresh(
    client, respx_mock
):
    operations = AnnotationOperations(client._http)
    route = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/annotations/mask-mutations:commit"
    ).mock(
        return_value=httpx.Response(
            428,
            json={
                "detail": {
                    "reason": "expected_versions_missing",
                    "current_scope_fingerprint": "f" * 64,
                    "current_versions": {str(ANNOTATION_ID): 5},
                }
            },
        )
    )
    request = MaskMutationCommitRequest(
        idempotency_key="mask-op-stale-scope-01",
        operation="split_components",
        scope=MaskMutationScope(media="image", class_name="car"),
        scope_fingerprint="e" * 64,
        expected_versions=[MaskExpectedVersion(annotation_id=ANNOTATION_ID, version=3)],
        mutations=[MaskDeleteMutation(kind="delete", annotation_id=ANNOTATION_ID)],
    )
    with pytest.raises(APIStatusError) as caught:
        operations.commit_mask_mutations(TASK_ID, request)
    assert caught.value.status_code == 428
    assert caught.value.detail["current_scope_fingerprint"] == "f" * 64
    assert len(route.calls) == 1


def test_polygon_slice_receipt_restores_with_exact_operation_and_version_pair(
    client, respx_mock
):
    operations = AnnotationOperations(client._http)
    operation_id = uuid4()
    commit = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/annotations/polygon-slices:commit"
    ).mock(
        return_value=httpx.Response(200, json=_slice_receipt(operation_id=operation_id))
    )
    restore = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/annotations/slices/{operation_id}:restore"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                **_slice_receipt(operation_id=uuid4()),
                "slice_operation_id": str(operation_id),
                "target": "before",
                "result_versions": {str(ANNOTATION_ID): 5, str(ANNOTATION_ID_2): 2},
                "no_op": False,
            },
        )
    )

    receipt = operations.commit_polygon_slice(
        TASK_ID,
        PolygonSliceCommitRequest(
            annotation_id=ANNOTATION_ID,
            expected_version=3,
            idempotency_key="slice-commit-key-01",
            cut_path=[(0.1, 0.2), (0.8, 0.7)],
        ),
    )
    assert receipt.slice_operation_id == operation_id
    assert json.loads(commit.calls.last.request.content)["cut_path"] == [
        [0.1, 0.2],
        [0.8, 0.7],
    ]

    restored = operations.restore_slice(
        TASK_ID,
        receipt.slice_operation_id,
        AnnotationSliceRestoreRequest(
            target="before",
            expected_versions={ANNOTATION_ID: 4, ANNOTATION_ID_2: 1},
            idempotency_key="slice-restore-key-01",
        ),
    )
    assert restored.target == "before"
    assert restored.slice_operation_id == operation_id
    assert restore.calls.last.request.url.path.endswith(
        f"/annotations/slices/{operation_id}:restore"
    )
    assert json.loads(restore.calls.last.request.content)["expected_versions"] == {
        str(ANNOTATION_ID): 4,
        str(ANNOTATION_ID_2): 1,
    }


def test_prediction_decisions_preserve_original_shape_index_and_native_receipt(
    client, respx_mock
):
    operations = AnnotationOperations(client._http)
    accept = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/predictions/{PREDICTION_ID}/accept"
    ).mock(return_value=httpx.Response(200, json=[_annotation(version=4)]))
    reject = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/predictions/{PREDICTION_ID}/reject"
    ).mock(return_value=httpx.Response(204))

    accepted = operations.accept_prediction(
        TASK_ID,
        PREDICTION_ID,
        shape_index=2,
        video_segment_id=SEGMENT_ID,
        override_class_name="vehicle",
        attribute_overrides={"material": "metal"},
    )
    assert len(accepted) == 1 and accepted[0].version == 4
    assert dict(accept.calls.last.request.url.params) == {
        "shape_index": "2",
        "video_segment_id": str(SEGMENT_ID),
        "override_class_name": "vehicle",
    }
    assert json.loads(accept.calls.last.request.content) == {
        "attribute_overrides": {"material": "metal"}
    }

    operations.reject_prediction(TASK_ID, PREDICTION_ID, shape_index=2)
    assert reject.calls.last.request.url.params["shape_index"] == "2"
    assert not reject.calls.last.request.content


def test_native_ai_mask_accept_passes_if_match_without_exposing_receipt(
    client, respx_mock
):
    operations = AnnotationOperations(client._http)
    route = respx_mock.post(f"{API}/tasks/{TASK_ID}/ai-mask-candidates/accept").mock(
        return_value=httpx.Response(200, json=_ai_mask_accept_response())
    )
    receipt = "native-mask-receipt-private-value"
    request = _ai_mask_accept_request()
    assert receipt not in repr(request)

    accepted = operations.accept_ai_mask_candidate(TASK_ID, request, if_match=4)
    assert accepted.replayed is False and accepted.annotation.version == 5
    sent = json.loads(route.calls.last.request.content)
    assert route.calls.last.request.headers["If-Match"] == "4"
    assert sent["candidate"]["receipt"] == receipt
    assert sent["candidate"]["candidate_index"] == 2
    assert sent["candidate"]["candidate"]["value"]["rle"]["counts"] == [0, 1, 3]
    assert sent["target"]["source_version"] == 4


def test_native_mask_refine_without_if_match_returns_conflict_without_refresh(
    client, respx_mock
):
    operations = AnnotationOperations(client._http)
    route = respx_mock.post(f"{API}/tasks/{TASK_ID}/ai-mask-candidates/accept").mock(
        return_value=httpx.Response(
            428,
            json={
                "detail": {
                    "reason": "if_match_required",
                    "message": "refine target requires If-Match",
                }
            },
        )
    )
    with pytest.raises(APIStatusError) as caught:
        operations.accept_ai_mask_candidate(TASK_ID, _ai_mask_accept_request())
    assert caught.value.status_code == 428
    assert caught.value.detail["reason"] == "if_match_required"
    assert "If-Match" not in route.calls.last.request.headers
    assert len(route.calls) == 1


def test_annotation_operations_cli_confirms_before_conversion_mutation(
    client, respx_mock, tmp_path, monkeypatch
):
    request_file = tmp_path / "conversion-execute.json"
    request_file.write_text(
        json.dumps(
            {
                "plan_token": "cvp_" + "p" * 48,
                "idempotency_key": "conversion:cli-test-001",
                "confirm_replace": True,
            }
        ),
        encoding="utf-8",
    )
    route = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/annotation-conversions:execute"
    ).mock(return_value=httpx.Response(200, json=_conversion_result()))
    client.annotation_operations = AnnotationOperations(client._http)
    monkeypatch.setattr(operations_cli, "get_client", lambda _json: client)

    declined = runner.invoke(
        operations_cli.app,
        ["conversion-execute", str(TASK_ID), "--request", str(request_file), "--json"],
    )
    assert declined.exit_code == 2
    assert not route.called

    accepted = runner.invoke(
        operations_cli.app,
        [
            "conversion-execute",
            str(TASK_ID),
            "--request",
            str(request_file),
            "--yes",
            "--json",
        ],
    )
    assert accepted.exit_code == 0, accepted.output
    assert route.called
    assert json.loads(route.calls.last.request.content)["idempotency_key"] == (
        "conversion:cli-test-001"
    )
