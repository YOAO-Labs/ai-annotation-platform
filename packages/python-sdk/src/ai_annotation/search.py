"""Visibility-filtered global search."""

from __future__ import annotations

from ai_annotation._http import HttpTransport
from ai_annotation.search_models import SearchQuery, SearchResponse


class Search:
    def __init__(self, http: HttpTransport):
        self._http = http

    def query(self, q: str, *, limit: int = 5) -> SearchResponse:
        """Search visible projects, tasks, datasets, and members."""
        request = SearchQuery(q=q, limit=limit)
        response = self._http.request(
            "GET",
            "/search",
            params=request.model_dump(mode="json", exclude_unset=True),
        )
        return SearchResponse.model_validate(response.json())
