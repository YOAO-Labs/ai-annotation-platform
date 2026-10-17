"""Task-local annotation, scene-neighbor, mask-capability, and prediction reads."""

from __future__ import annotations

from uuid import UUID

from ai_annotation._http import HttpTransport, _drop_none
from ai_annotation.task_workflow_models import (
    NeighborAnnotations,
    TaskAnnotationPage,
    TaskMaskCapabilities,
    TaskNeighbors,
    TaskPrediction,
)

IdLike = str | UUID


class TaskContext:
    def __init__(self, http: HttpTransport):
        self._http = http

    def annotations_page(
        self,
        task_id: IdLike,
        *,
        limit: int = 200,
        cursor: str | None = None,
        video_segment_id: IdLike | None = None,
    ) -> TaskAnnotationPage:
        """Read one keyset page; video collaboration callers pass the exact segment."""
        resp = self._http.request(
            "GET",
            f"/tasks/{task_id}/annotations/page",
            params=_drop_none(
                {
                    "limit": limit,
                    "cursor": cursor,
                    "video_segment_id": (
                        str(video_segment_id) if video_segment_id is not None else None
                    ),
                }
            ),
        )
        return TaskAnnotationPage.model_validate(resp.json())

    def neighbors(self, task_id: IdLike, *, k: int = 1) -> TaskNeighbors:
        resp = self._http.request("GET", f"/tasks/{task_id}/neighbors", params={"k": k})
        return TaskNeighbors.model_validate(resp.json())

    def neighbor_annotations(
        self,
        task_id: IdLike,
        *,
        k: int = 1,
        track_id: str | None = None,
    ) -> NeighborAnnotations:
        resp = self._http.request(
            "GET",
            f"/tasks/{task_id}/neighbor-annotations",
            params=_drop_none({"k": k, "track_id": track_id}),
        )
        return NeighborAnnotations.model_validate(resp.json())

    def mask_capabilities(self, task_id: IdLike) -> TaskMaskCapabilities:
        resp = self._http.request("GET", f"/tasks/{task_id}/mask-capabilities")
        return TaskMaskCapabilities.model_validate(resp.json())

    def predictions(
        self,
        task_id: IdLike,
        *,
        model_version: str | None = None,
        min_confidence: float | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[TaskPrediction]:
        resp = self._http.request(
            "GET",
            f"/tasks/{task_id}/predictions",
            params=_drop_none(
                {
                    "model_version": model_version,
                    "min_confidence": min_confidence,
                    "limit": limit,
                    "offset": offset,
                }
            ),
        )
        return [TaskPrediction.model_validate(item) for item in resp.json()]
