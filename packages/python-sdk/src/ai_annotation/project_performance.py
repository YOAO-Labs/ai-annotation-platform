"""Project-scoped member performance reads and CSV export."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from ai_annotation._http import HttpTransport
from ai_annotation.project_performance_models import (
    ProjectMemberPerformanceDetail,
    ProjectMemberPerformanceEvents,
    ProjectMembersPerformance,
    ProjectPerformanceQuery,
)

IdLike = str | UUID


def _params(query: ProjectPerformanceQuery | None) -> dict[str, object]:
    if query is None:
        return {}
    return query.model_dump(
        mode="json", by_alias=True, exclude_unset=True, exclude_none=True
    )


class ProjectPerformance:
    """Performance data for project owners and super administrators."""

    def __init__(self, http: HttpTransport):
        self._http = http

    def members(
        self,
        project_id: IdLike,
        query: ProjectPerformanceQuery | None = None,
    ) -> ProjectMembersPerformance:
        """Read one cursor page of project members and performance totals."""
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/performance/members",
            params=_params(query),
        )
        return ProjectMembersPerformance.model_validate(response.json())

    def member(
        self,
        project_id: IdLike,
        member_id: IdLike,
        query: ProjectPerformanceQuery | None = None,
    ) -> ProjectMemberPerformanceDetail:
        """Read one member's metrics, trend, distributions, and evidence page."""
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/performance/members/{member_id}",
            params=_params(query),
        )
        return ProjectMemberPerformanceDetail.model_validate(response.json())

    def events(
        self,
        project_id: IdLike,
        member_id: IdLike,
        query: ProjectPerformanceQuery | None = None,
    ) -> ProjectMemberPerformanceEvents:
        """Read a member's cursor-paged performance evidence."""
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/performance/members/{member_id}/events",
            params=_params(query),
        )
        return ProjectMemberPerformanceEvents.model_validate(response.json())

    def export_members(
        self,
        project_id: IdLike,
        dest: str | Path,
        query: ProjectPerformanceQuery | None = None,
    ) -> Path:
        """Stream the full filtered member report as CSV to an existing directory."""
        params = _params(query)
        # The export endpoint always emits the full matching roster.
        params.pop("cursor", None)
        params.pop("limit", None)
        return self._http.stream_download(
            f"/api/v1/projects/{project_id}/performance/export",
            Path(dest),
            params=params,
        )
