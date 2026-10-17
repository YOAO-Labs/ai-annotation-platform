"""Explicit, typed task-scoped annotation transformation operations."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ai_annotation._http import HttpTransport, _drop_none
from ai_annotation.annotation_operation_models import (
    AiMaskAcceptRequest,
    AiMaskAcceptResponse,
    AnnotationConversionDryRunRequest,
    AnnotationConversionDryRunResponse,
    AnnotationConversionExecuteRequest,
    AnnotationConversionExecuteResponse,
    AnnotationSliceResponse,
    AnnotationSliceRestoreRequest,
    InterpolateRangeRequest,
    InterpolateRangeResponse,
    MaskMutationCommitRequest,
    MaskMutationCommitResponse,
    PolygonSliceCommitRequest,
    PropagateBatchRequest,
    PropagateBatchResponse,
    PropagateRequest,
    PropagateResponse,
    SecondaryInferenceRequest,
    SecondaryInferenceResponse,
)
from ai_annotation.models import Annotation

IdLike = str | UUID


def _request_body(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_unset=True)


class AnnotationOperations:
    """Task- and annotation-scoped transforms backed by server validation.

    This resource keeps proofs and concurrency inputs explicit: conversion plans,
    slice receipts, whole-scope mask fingerprints/versions, inference receipts and
    idempotency keys are supplied by the caller and are never refreshed or invented.
    The API validates every task/annotation relationship and caller's write access.
    """

    def __init__(self, http: HttpTransport):
        self._http = http

    def conversion_dry_run(
        self,
        task_id: IdLike,
        request: AnnotationConversionDryRunRequest,
    ) -> AnnotationConversionDryRunResponse:
        """Build a short-lived, actor/task-bound conversion plan (about ten minutes)."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/annotation-conversions:dry-run",
            json=_request_body(request),
        )
        return AnnotationConversionDryRunResponse.model_validate(response.json())

    def conversion_execute(
        self,
        task_id: IdLike,
        request: AnnotationConversionExecuteRequest,
    ) -> AnnotationConversionExecuteResponse:
        """Execute exactly the supplied plan token and caller idempotency key."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/annotation-conversions:execute",
            json=_request_body(request),
        )
        return AnnotationConversionExecuteResponse.model_validate(response.json())

    def interpolate_range(
        self,
        task_id: IdLike,
        request: InterpolateRangeRequest,
    ) -> InterpolateRangeResponse:
        """Interpolate the track between this start task and ``request.to_task_id``."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/annotations/interpolate-range",
            json=_request_body(request),
        )
        return InterpolateRangeResponse.model_validate(response.json())

    def propagate_to_task(
        self,
        task_id: IdLike,
        annotation_id: IdLike,
        request: PropagateRequest,
    ) -> PropagateResponse:
        """Copy one source annotation to a visible, editable target task."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/annotations/{annotation_id}/propagate-to-task",
            json=_request_body(request),
        )
        return PropagateResponse.model_validate(response.json())

    def propagate_batch(
        self,
        task_id: IdLike,
        request: PropagateBatchRequest,
    ) -> PropagateBatchResponse:
        """Propagate selected source annotations, or all eligible sources when null."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/annotations/propagate-batch",
            json=_request_body(request),
        )
        return PropagateBatchResponse.model_validate(response.json())

    def secondary_inference(
        self,
        task_id: IdLike,
        annotation_id: IdLike,
        request: SecondaryInferenceRequest,
    ) -> SecondaryInferenceResponse:
        """Run a synchronous backend stage; preserve dynamic params and variants."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/annotations/{annotation_id}/secondary-inference",
            json=_request_body(request),
        )
        return SecondaryInferenceResponse.model_validate(response.json())

    def commit_mask_mutations(
        self,
        task_id: IdLike,
        request: MaskMutationCommitRequest,
    ) -> MaskMutationCommitResponse:
        """Commit one atomic mask operation with its supplied scope proof and versions."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/annotations/mask-mutations:commit",
            json=_request_body(request),
        )
        return MaskMutationCommitResponse.model_validate(response.json())

    def commit_polygon_slice(
        self,
        task_id: IdLike,
        request: PolygonSliceCommitRequest,
    ) -> AnnotationSliceResponse:
        """Commit an image polygon split and return its versioned restore receipt."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/annotations/polygon-slices:commit",
            json=_request_body(request),
        )
        return AnnotationSliceResponse.model_validate(response.json())

    def restore_slice(
        self,
        task_id: IdLike,
        operation_id: IdLike,
        request: AnnotationSliceRestoreRequest,
    ) -> AnnotationSliceResponse:
        """Restore to before/after using the complete receipt version set.

        ``operation_id`` is the receipt's ``slice_operation_id``. The original slice
        restore window is 30 days; version drift and expired receipts remain API errors.
        """
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/annotations/slices/{operation_id}:restore",
            json=_request_body(request),
        )
        return AnnotationSliceResponse.model_validate(response.json())

    def accept_prediction(
        self,
        task_id: IdLike,
        prediction_id: IdLike,
        *,
        shape_index: int | None = None,
        video_segment_id: IdLike | None = None,
        override_class_name: str | None = None,
        attribute_overrides: dict[str, Any] | None = None,
    ) -> list[Annotation]:
        """Accept a prediction shape, using its original ``result.shape_index``.

        These decision endpoints do not accept a caller idempotency key. After an
        uncertain network result, read annotations/predictions before deciding whether
        to submit another decision.
        """
        params = _drop_none(
            {
                "shape_index": shape_index,
                "video_segment_id": (
                    str(video_segment_id) if video_segment_id is not None else None
                ),
                "override_class_name": override_class_name,
            }
        )
        kwargs: dict[str, Any] = {"params": params}
        if attribute_overrides is not None:
            kwargs["json"] = {"attribute_overrides": attribute_overrides}
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/predictions/{prediction_id}/accept",
            **kwargs,
        )
        return [Annotation.model_validate(item) for item in response.json()]

    def reject_prediction(
        self,
        task_id: IdLike,
        prediction_id: IdLike,
        *,
        shape_index: int | None = None,
    ) -> None:
        """Soft-reject one original shape index, or all shapes when omitted."""
        self._http.request(
            "POST",
            f"/tasks/{task_id}/predictions/{prediction_id}/reject",
            params=_drop_none({"shape_index": shape_index}),
        )

    def accept_ai_mask_candidate(
        self,
        task_id: IdLike,
        request: AiMaskAcceptRequest,
        *,
        if_match: int | None = None,
    ) -> AiMaskAcceptResponse:
        """Accept a receipt-backed native candidate; refine requests need If-Match.

        The API checks that If-Match matches ``request.target.source_version`` and the
        current row. The SDK forwards it unchanged and never refreshes a stale proof.
        """
        headers = {"If-Match": str(if_match)} if if_match is not None else None
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/ai-mask-candidates/accept",
            json=_request_body(request),
            headers=headers,
        )
        return AiMaskAcceptResponse.model_validate(response.json())


__all__ = ["AnnotationOperations"]
