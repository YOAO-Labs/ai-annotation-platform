"""Typed access to annotation lifecycle and task audit timelines."""

from __future__ import annotations

from uuid import UUID

from ai_annotation._http import HttpTransport
from ai_annotation.history_models import AnnotationHistoryResponse

IdLike = str | UUID


class AnnotationHistory:
    """Read annotation or task history; API visibility errors pass through unchanged."""

    def __init__(self, http: HttpTransport):
        self._http = http

    def annotation(self, annotation_id: IdLike) -> AnnotationHistoryResponse:
        response = self._http.request("GET", f"/annotations/{annotation_id}/history")
        return AnnotationHistoryResponse.model_validate(response.json())

    def task(self, task_id: IdLike) -> AnnotationHistoryResponse:
        response = self._http.request("GET", f"/tasks/{task_id}/audit-history")
        return AnnotationHistoryResponse.model_validate(response.json())


__all__ = ["AnnotationHistory"]
