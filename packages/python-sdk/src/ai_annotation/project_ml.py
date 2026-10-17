"""Project-scoped ML registration, inference and runtime operations."""

from __future__ import annotations

import json
from uuid import UUID

from ai_annotation._http import HttpTransport
from ai_annotation.models import MLBackendUnloadResult
from ai_annotation.project_ml_models import (
    ProjectMLBackendCapabilities,
    ProjectMLBackendCreateRequest,
    ProjectMLBackendOut,
    ProjectMLBackendReloadRequest,
    ProjectMLBackendUpdateRequest,
    ProjectMLFrameInteractiveRequest,
    ProjectMLFramePredictRequest,
    ProjectMLInteractiveRequest,
    ProjectMLInteractiveResponse,
    ProjectMLPredictFrameResponse,
    ProjectMLPredictTestResponse,
    ProjectMLWarmupRequest,
)

IdLike = str | UUID


class ProjectML:
    """Uncovered ML backend routes; existing `Client.ml_backends` APIs are unchanged."""

    def __init__(self, http: HttpTransport):
        self._http = http

    def create_backend(
        self, project_id: IdLike, body: ProjectMLBackendCreateRequest
    ) -> ProjectMLBackendOut:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/ml-backends",
            json=body.model_dump(mode="json", exclude_unset=True),
        )
        return ProjectMLBackendOut.model_validate(response.json())

    def update_backend(
        self,
        project_id: IdLike,
        backend_id: IdLike,
        body: ProjectMLBackendUpdateRequest,
    ) -> ProjectMLBackendOut:
        response = self._http.request(
            "PUT",
            f"/projects/{project_id}/ml-backends/{backend_id}",
            json=body.model_dump(mode="json", exclude_unset=True),
        )
        return ProjectMLBackendOut.model_validate(response.json())

    def disable_backend(self, project_id: IdLike, backend_id: IdLike) -> None:
        """Disable the backend for this project; the global registry record remains."""
        self._http.request("DELETE", f"/projects/{project_id}/ml-backends/{backend_id}")

    def setup(self, project_id: IdLike, backend_id: IdLike) -> dict:
        """Fetch upstream /setup JSON; server caches it for up to 30 seconds."""
        response = self._http.request(
            "GET", f"/projects/{project_id}/ml-backends/{backend_id}/setup"
        )
        return response.json()

    def capabilities(
        self, project_id: IdLike, backend_id: IdLike
    ) -> ProjectMLBackendCapabilities:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/ml-backends/{backend_id}/capabilities",
        )
        return ProjectMLBackendCapabilities.model_validate(response.json())

    def refresh_capabilities(
        self, project_id: IdLike, backend_id: IdLike
    ) -> ProjectMLBackendCapabilities:
        """Invalidate setup cache and re-read backend capability declarations."""
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/ml-backends/{backend_id}/capabilities/refresh",
        )
        return ProjectMLBackendCapabilities.model_validate(response.json())

    def interactive(
        self,
        project_id: IdLike,
        backend_id: IdLike,
        body: ProjectMLInteractiveRequest,
    ) -> ProjectMLInteractiveResponse:
        """Run interactive inference and return transient candidates without saving them."""
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/ml-backends/{backend_id}/interactive-annotating",
            json=body.model_dump(mode="json", exclude_unset=True),
        )
        return ProjectMLInteractiveResponse.model_validate(response.json())

    def interactive_frame(
        self,
        project_id: IdLike,
        backend_id: IdLike,
        body: ProjectMLFrameInteractiveRequest,
    ) -> ProjectMLInteractiveResponse:
        """Upload one frame and return transient interactive candidates."""
        context = json.dumps(
            body.context, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        with body.frame_path.open("rb") as frame:
            response = self._http.request(
                "POST",
                f"/projects/{project_id}/ml-backends/{backend_id}/interactive-annotating-frame",
                data={
                    "task_id": str(body.task_id),
                    "frame_index": str(body.frame_index),
                    "context": context,
                },
                files={"frame": (body.frame_path.name, frame)},
            )
        return ProjectMLInteractiveResponse.model_validate(response.json())

    def predict_frame(
        self,
        project_id: IdLike,
        backend_id: IdLike,
        body: ProjectMLFramePredictRequest,
    ) -> ProjectMLPredictFrameResponse:
        """Upload one frame, run prediction, and persist a pending Prediction record."""
        config = json.dumps(
            body.config, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        with body.frame_path.open("rb") as frame:
            response = self._http.request(
                "POST",
                f"/projects/{project_id}/ml-backends/{backend_id}/predict-frame",
                data={
                    "task_id": str(body.task_id),
                    "frame_index": str(body.frame_index),
                    "config": config,
                },
                files={"frame": (body.frame_path.name, frame)},
            )
        return ProjectMLPredictFrameResponse.model_validate(response.json())

    def predict_test(
        self, project_id: IdLike, backend_id: IdLike, task_id: IdLike
    ) -> ProjectMLPredictTestResponse:
        """Run one task through the backend; returns candidates without persisting them."""
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/ml-backends/{backend_id}/predict-test",
            params={"task_id": str(task_id)},
        )
        return ProjectMLPredictTestResponse.model_validate(response.json())

    def reload(
        self,
        project_id: IdLike,
        backend_id: IdLike,
        body: ProjectMLBackendReloadRequest | None = None,
    ) -> dict:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/ml-backends/{backend_id}/reload",
            json=body.model_dump(mode="json", exclude_unset=True) if body else None,
        )
        return response.json()

    def unload(self, project_id: IdLike, backend_id: IdLike) -> MLBackendUnloadResult:
        response = self._http.request(
            "POST", f"/projects/{project_id}/ml-backends/{backend_id}/unload"
        )
        return MLBackendUnloadResult.model_validate(response.json())

    def warmup(
        self,
        project_id: IdLike,
        backend_id: IdLike,
        body: ProjectMLWarmupRequest | None = None,
    ) -> dict:
        """Send backend-specific warmup JSON; may change shared model residency."""
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/ml-backends/{backend_id}/warmup",
            json=body.model_dump(mode="json") if body else None,
        )
        return response.json()
