"""Project template list, CRUD, and duplication API."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from ai_annotation._http import HttpTransport
from ai_annotation.project_template_models import (
    ProjectTemplate,
    ProjectTemplateCreate,
    ProjectTemplateUpdate,
    TemplateScope,
)

IdLike = str | UUID


class ProjectTemplates:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(
        self,
        *,
        scope: TemplateScope | None = None,
        type_key: Sequence[str] | None = None,
        search: str | None = None,
    ) -> list[ProjectTemplate]:
        """List visible templates; the API returns an unpaginated array."""
        params: dict[str, Any] = {
            key: value
            for key, value in {
                "scope": scope,
                "type_key": type_key,
                "search": search,
            }.items()
            if value is not None
        }
        resp = self._http.request("GET", "/project-templates", params=params)
        return [ProjectTemplate.model_validate(item) for item in resp.json()]

    def get(self, template_id: IdLike) -> ProjectTemplate:
        resp = self._http.request("GET", f"/project-templates/{template_id}")
        return ProjectTemplate.model_validate(resp.json())

    def create(self, body: ProjectTemplateCreate) -> ProjectTemplate:
        resp = self._http.request(
            "POST",
            "/project-templates",
            json=body.model_dump(mode="json", exclude_unset=True),
        )
        return ProjectTemplate.model_validate(resp.json())

    def update(
        self, template_id: IdLike, body: ProjectTemplateUpdate
    ) -> ProjectTemplate:
        resp = self._http.request(
            "PATCH",
            f"/project-templates/{template_id}",
            json=body.model_dump(mode="json", exclude_unset=True),
        )
        return ProjectTemplate.model_validate(resp.json())

    def delete(self, template_id: IdLike) -> None:
        self._http.request("DELETE", f"/project-templates/{template_id}")

    def duplicate(self, template_id: IdLike) -> ProjectTemplate:
        resp = self._http.request("POST", f"/project-templates/{template_id}/duplicate")
        return ProjectTemplate.model_validate(resp.json())
