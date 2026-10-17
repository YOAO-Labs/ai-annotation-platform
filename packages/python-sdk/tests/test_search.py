"""Global search remains visibility-filtered and query-bounded by the API."""

from __future__ import annotations

import pytest
import httpx
from pydantic import ValidationError

from ai_annotation.search import Search

from .conftest import API


def test_search_encodes_query_and_parses_all_result_groups(client, respx_mock):
    route = respx_mock.get(f"{API}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "projects": [
                    {
                        "id": "project-1",
                        "display_id": "P-1",
                        "name": "objects",
                        "type_key": "object_detection",
                        "data_type": "image",
                        "type_label": "Detection",
                    }
                ],
                "tasks": [
                    {
                        "id": "task-1",
                        "display_id": "T-1",
                        "file_name": "objects.png",
                        "project_id": "project-1",
                        "project_name": "objects",
                    }
                ],
                "datasets": [
                    {"id": "dataset-1", "name": "objects", "data_type": "image"}
                ],
                "members": [
                    {
                        "id": "user-1",
                        "name": "Ada",
                        "email": "ada@example.test",
                        "role": "employee",
                    }
                ],
            },
        )
    )

    result = Search(client._http).query("Ada & Grace", limit=7)

    assert result.projects[0].type_label == "Detection"
    assert result.tasks[0].project_name == "objects"
    assert result.datasets[0].name == "objects"
    assert result.members[0].email == "ada@example.test"
    assert route.calls.last.request.url.params["q"] == "Ada & Grace"
    assert route.calls.last.request.url.params["limit"] == "7"


@pytest.mark.parametrize(("q", "limit"), [("", 5), ("x", 0), ("x", 21)])
def test_search_rejects_query_values_outside_api_bounds(client, respx_mock, q, limit):
    route = respx_mock.get(f"{API}/search")

    with pytest.raises(ValidationError):
        Search(client._http).query(q, limit=limit)

    assert not route.called
