"""Project-scoped Data Manager queries and explicitly selected task actions."""

from __future__ import annotations

from urllib.parse import quote
from uuid import UUID

from ai_annotation._http import HttpTransport
from ai_annotation.data_manager_models import (
    DataManagerEntityLocation,
    DataManagerEntityQueryRequest,
    DataManagerEntityScope,
    DataManagerMatchesRequest,
    DataManagerMatchesResponse,
    DataManagerObjectDetailResponse,
    DataManagerObjectQueryResponse,
    DataManagerSchemaResponse,
    DataManagerSummaryRequest,
    DataManagerSummaryResponse,
    DataManagerTaskAssignmentApplyRequest,
    DataManagerTaskAssignmentRequest,
    DataManagerTaskAssignmentResponse,
    DataManagerTaskExportRequest,
    DataManagerTaskExportSubmission,
    DataManagerTrackDetailResponse,
    DataManagerTrackQueryResponse,
)

IdLike = str | UUID


class DataManager:
    def __init__(self, http: HttpTransport):
        self._http = http

    def schema(
        self, project_id: IdLike, *, entity_scope: DataManagerEntityScope = "tasks"
    ) -> DataManagerSchemaResponse:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/data-manager/schema",
            params={"entity_scope": entity_scope},
        )
        return DataManagerSchemaResponse.model_validate(response.json())

    def summary(
        self, project_id: IdLike, body: DataManagerSummaryRequest
    ) -> DataManagerSummaryResponse:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/data-manager/summary",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return DataManagerSummaryResponse.model_validate(response.json())

    def matches(
        self, project_id: IdLike, task_id: IdLike, body: DataManagerMatchesRequest
    ) -> DataManagerMatchesResponse:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/tasks/{task_id}/data-manager/matches",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return DataManagerMatchesResponse.model_validate(response.json())

    def query_objects(
        self, project_id: IdLike, body: DataManagerEntityQueryRequest
    ) -> DataManagerObjectQueryResponse:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/data-manager/objects/query",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return DataManagerObjectQueryResponse.model_validate(response.json())

    def object_detail(
        self, project_id: IdLike, annotation_id: IdLike
    ) -> DataManagerObjectDetailResponse:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/data-manager/objects/{annotation_id}/detail",
        )
        return DataManagerObjectDetailResponse.model_validate(response.json())

    def object_location(
        self, project_id: IdLike, annotation_id: IdLike
    ) -> DataManagerEntityLocation:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/data-manager/objects/{annotation_id}/location",
        )
        return DataManagerEntityLocation.model_validate(response.json())

    def query_tracks(
        self, project_id: IdLike, body: DataManagerEntityQueryRequest
    ) -> DataManagerTrackQueryResponse:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/data-manager/tracks/query",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return DataManagerTrackQueryResponse.model_validate(response.json())

    def track_detail(
        self, project_id: IdLike, track_ref: str
    ) -> DataManagerTrackDetailResponse:
        track_ref = quote(track_ref, safe="")
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/data-manager/tracks/{track_ref}/detail",
        )
        return DataManagerTrackDetailResponse.model_validate(response.json())

    def assignment_preview(
        self, project_id: IdLike, body: DataManagerTaskAssignmentRequest
    ) -> DataManagerTaskAssignmentResponse:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/data-manager/tasks/assignment-preview",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return DataManagerTaskAssignmentResponse.model_validate(response.json())

    def assignment_apply(
        self, project_id: IdLike, body: DataManagerTaskAssignmentApplyRequest
    ) -> DataManagerTaskAssignmentResponse:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/data-manager/tasks/assignment-apply",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return DataManagerTaskAssignmentResponse.model_validate(response.json())

    def export_tasks(
        self,
        project_id: IdLike,
        body: DataManagerTaskExportRequest,
        *,
        idempotency_key: str | None = None,
    ) -> DataManagerTaskExportSubmission:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/data-manager/tasks/export",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
            headers={"Idempotency-Key": idempotency_key}
            if idempotency_key is not None
            else None,
        )
        return DataManagerTaskExportSubmission.model_validate(response.json())
