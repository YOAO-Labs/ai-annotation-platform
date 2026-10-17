"""Data-group REST resources."""

from __future__ import annotations

from uuid import UUID

from ai_annotation._http import HttpTransport, _drop_none
from ai_annotation.user_management_models import (
    Group,
    GroupCreate,
    GroupPage,
    GroupUpdate,
)

IdLike = str | UUID


class Groups:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(self) -> list[Group]:
        """Read the legacy array endpoint."""
        response = self._http.request("GET", "/groups")
        return [Group.model_validate(item) for item in response.json()]

    def query(
        self, *, page: int = 1, page_size: int = 50, search: str | None = None
    ) -> GroupPage:
        response = self._http.request(
            "GET",
            "/groups/query",
            params=_drop_none({"page": page, "page_size": page_size, "search": search}),
        )
        return GroupPage.model_validate(response.json())

    def create(self, request: GroupCreate) -> Group:
        response = self._http.request(
            "POST",
            "/groups",
            json=request.model_dump(mode="json", exclude_unset=True, exclude_none=True),
        )
        return Group.model_validate(response.json())

    def update(self, group_id: IdLike, request: GroupUpdate) -> Group:
        response = self._http.request(
            "PATCH",
            f"/groups/{group_id}",
            json=request.model_dump(mode="json", exclude_unset=True),
        )
        return Group.model_validate(response.json())

    def delete(self, group_id: IdLike) -> None:
        self._http.request("DELETE", f"/groups/{group_id}")
