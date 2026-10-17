"""Named project pipelines and asynchronous preannotation resources."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel

from ai_annotation._http import HttpTransport, _drop_none
from ai_annotation.pipeline_models import (
    PreannotateRequest,
    PreannotationSubmission,
    PipelineScope,
    ProjectPipeline,
    ProjectPipelineApplyRequest,
    ProjectPipelineCreate,
    ProjectPipelineUpdate,
)

IdLike = str | UUID


def _body(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_unset=True)


class ProjectPipelines:
    """Client methods for the named-pipeline and preannotation API routes."""

    def __init__(self, http: HttpTransport):
        self._http = http

    def list(
        self,
        *,
        scope: PipelineScope | None = None,
        project_id: IdLike | None = None,
        organization_id: IdLike | None = None,
    ) -> list[ProjectPipeline]:
        response = self._http.request(
            "GET",
            "/project-pipelines",
            params=_drop_none(
                {
                    "scope": scope,
                    "project_id": str(project_id) if project_id is not None else None,
                    "organization_id": (
                        str(organization_id) if organization_id is not None else None
                    ),
                }
            ),
        )
        return [ProjectPipeline.model_validate(row) for row in response.json()]

    def get(self, pipeline_id: IdLike) -> ProjectPipeline:
        response = self._http.request("GET", f"/project-pipelines/{pipeline_id}")
        return ProjectPipeline.model_validate(response.json())

    def create(self, body: ProjectPipelineCreate) -> ProjectPipeline:
        response = self._http.request("POST", "/project-pipelines", json=_body(body))
        return ProjectPipeline.model_validate(response.json())

    def update(
        self, pipeline_id: IdLike, body: ProjectPipelineUpdate
    ) -> ProjectPipeline:
        response = self._http.request(
            "PUT", f"/project-pipelines/{pipeline_id}", json=_body(body)
        )
        return ProjectPipeline.model_validate(response.json())

    def delete(self, pipeline_id: IdLike) -> None:
        self._http.request("DELETE", f"/project-pipelines/{pipeline_id}")

    def apply(
        self, project_id: IdLike, body: ProjectPipelineApplyRequest
    ) -> ProjectPipeline:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/pipelines/apply",
            json=_body(body),
        )
        return ProjectPipeline.model_validate(response.json())

    def preannotate(
        self,
        project_id: IdLike,
        body: PreannotateRequest,
        *,
        idempotency_key: str | None = None,
    ) -> PreannotationSubmission:
        headers = (
            {"Idempotency-Key": idempotency_key}
            if idempotency_key is not None
            else None
        )
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/preannotate",
            json=_body(body),
            headers=headers,
        )
        return PreannotationSubmission.model_validate(response.json())
