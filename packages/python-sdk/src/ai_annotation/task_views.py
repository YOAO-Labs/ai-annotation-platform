"""Project saved-view and task-query operations."""

from __future__ import annotations

from uuid import UUID

from ai_annotation._http import HttpTransport
from ai_annotation.task_view_models import (
    ProjectTaskQueryRequest,
    ProjectTaskQueryResponse,
    ProjectTaskView,
    ProjectTaskViewCopyRequest,
    ProjectTaskViewCreate,
    ProjectTaskViewListResponse,
    ProjectTaskViewUpdate,
    TaskViewEntityScope,
)

IdLike = str | UUID


class TaskViews:
    """Manage saved task views and query tasks within a project."""

    def __init__(self, http: HttpTransport):
        self._http = http

    def list(
        self,
        project_id: IdLike,
        entity_scope: TaskViewEntityScope = "tasks",
    ) -> ProjectTaskViewListResponse:
        """List builtin and saved views visible to the authenticated user."""
        if entity_scope not in {"tasks", "objects", "tracks"}:
            raise ValueError("entity_scope must be tasks, objects, or tracks")
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/task-views",
            params={"entity_scope": entity_scope},
        )
        return ProjectTaskViewListResponse.model_validate(response.json())

    def create(
        self,
        project_id: IdLike,
        view: ProjectTaskViewCreate,
    ) -> ProjectTaskView:
        """Create a saved view; shared views require project management access."""
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/task-views",
            json=view.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return ProjectTaskView.model_validate(response.json())

    def get(self, project_id: IdLike, view_id: IdLike) -> ProjectTaskView:
        """Get a view visible to the authenticated project member."""
        response = self._http.request(
            "GET", f"/projects/{project_id}/task-views/{view_id}"
        )
        return ProjectTaskView.model_validate(response.json())

    def update(
        self,
        project_id: IdLike,
        view_id: IdLike,
        view: ProjectTaskViewUpdate,
    ) -> ProjectTaskView:
        """Patch an owned private view or a shared view as a project manager.

        Omitted fields stay unchanged. The API treats explicit null the same way;
        use empty objects and lists to clear filter, sort, or column settings.
        """
        response = self._http.request(
            "PATCH",
            f"/projects/{project_id}/task-views/{view_id}",
            json=view.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return ProjectTaskView.model_validate(response.json())

    def delete(self, project_id: IdLike, view_id: IdLike) -> None:
        """Delete a saved view configuration; this does not delete task data."""
        self._http.request("DELETE", f"/projects/{project_id}/task-views/{view_id}")

    def copy(
        self,
        project_id: IdLike,
        view_id: IdLike,
        request: ProjectTaskViewCopyRequest,
    ) -> ProjectTaskView:
        """Copy a visible saved view; set visibility='private' to copy shared views."""
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/task-views/{view_id}/copy",
            json=request.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return ProjectTaskView.model_validate(response.json())

    def query(
        self,
        project_id: IdLike,
        request: ProjectTaskQueryRequest,
    ) -> ProjectTaskQueryResponse:
        """Run a read-only project task query with a dynamic filter JSON tree."""
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/tasks/query",
            json=request.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return ProjectTaskQueryResponse.model_validate(response.json())

    def query_view(
        self,
        project_id: IdLike,
        view_id: IdLike,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> ProjectTaskQueryResponse:
        """Query one saved task view using offset pagination."""
        page = ProjectTaskQueryRequest(limit=limit, offset=offset)
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/task-views/{view_id}/tasks",
            params={"limit": page.limit, "offset": page.offset},
        )
        return ProjectTaskQueryResponse.model_validate(response.json())
