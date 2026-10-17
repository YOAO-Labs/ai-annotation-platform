"""Contracts for saved task views and the project task query."""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from ai_annotation.task_view_models import (
    ProjectTaskQueryRequest,
    ProjectTaskViewCopyRequest,
    ProjectTaskViewCreate,
    ProjectTaskViewUpdate,
)
from ai_annotation.task_views import TaskViews

from .conftest import API

PROJECT = "11111111-1111-4111-8111-111111111111"
VIEW = "22222222-2222-4222-8222-222222222222"
COPY = "33333333-3333-4333-8333-333333333333"
TASK = "44444444-4444-4444-8444-444444444444"
NOW = "2026-10-17T00:00:00Z"


def _view(view_id: str = VIEW, **updates):
    result = {
        "id": view_id,
        "key": None,
        "project_id": PROJECT,
        "owner_id": PROJECT,
        "name": "Review queue",
        "visibility": "private",
        "entity_scope": "tasks",
        "filter_json": {},
        "sort_json": [],
        "columns_json": [],
        "builtin": False,
        "task_count": 2,
        "result_count": 2,
        "created_at": NOW,
        "updated_at": NOW,
        "invalid_fields": [],
    }
    result.update(updates)
    return result


def _query_page():
    return {
        "items": [
            {
                "id": TASK,
                "project_id": PROJECT,
                "display_id": "T-1",
                "file_name": "one.png",
                "file_type": "image/png",
                "status": "pending",
                "created_at": NOW,
                "annotation_count": 3,
                "prediction_count": 1,
                "unresolved_issue_count": 0,
                "model_versions": ["m1"],
            }
        ],
        "total": 1,
        "limit": 10,
        "offset": 0,
    }


def test_task_view_resource_calls_all_eight_routes(client, respx_mock):
    views = TaskViews(client._http)
    dynamic_filter = {
        "op": "and",
        "rules": [
            {
                "field": "annotation.attribute.bbox.color",
                "op": "eq",
                "value": "red",
            }
        ],
    }
    list_route = respx_mock.get(f"{API}/projects/{PROJECT}/task-views").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    _view(
                        None,
                        key="all",
                        owner_id=None,
                        name="All tasks",
                        builtin=True,
                        task_count=None,
                        result_count=4,
                        created_at=None,
                        updated_at=None,
                    )
                ]
            },
        )
    )
    create_route = respx_mock.post(f"{API}/projects/{PROJECT}/task-views").mock(
        return_value=httpx.Response(201, json=_view(filter_json=dynamic_filter))
    )
    respx_mock.get(f"{API}/projects/{PROJECT}/task-views/{VIEW}").mock(
        return_value=httpx.Response(200, json=_view(filter_json=dynamic_filter))
    )
    update_route = respx_mock.patch(f"{API}/projects/{PROJECT}/task-views/{VIEW}").mock(
        return_value=httpx.Response(200, json=_view(filter_json={}))
    )
    delete_route = respx_mock.delete(
        f"{API}/projects/{PROJECT}/task-views/{VIEW}"
    ).mock(return_value=httpx.Response(204))
    copy_route = respx_mock.post(
        f"{API}/projects/{PROJECT}/task-views/{VIEW}/copy"
    ).mock(return_value=httpx.Response(201, json=_view(COPY, name="Review queue copy")))
    query_route = respx_mock.post(f"{API}/projects/{PROJECT}/tasks/query").mock(
        return_value=httpx.Response(200, json=_query_page())
    )
    query_view_route = respx_mock.get(
        f"{API}/projects/{PROJECT}/task-views/{VIEW}/tasks"
    ).mock(return_value=httpx.Response(200, json=_query_page()))

    listed = views.list(PROJECT)
    assert listed.items[0].id is None
    assert listed.items[0].key == "all"
    assert listed.items[0].result_count == 4
    assert list_route.calls.last.request.url.params["entity_scope"] == "tasks"

    created = views.create(
        PROJECT, ProjectTaskViewCreate(name="Review queue", filter_json=dynamic_filter)
    )
    assert created.filter_json == dynamic_filter
    assert (
        create_route.calls.last.request.read()
        == b'{"name":"Review queue","filter_json":{"op":"and","rules":[{"field":"annotation.attribute.bbox.color","op":"eq","value":"red"}]}}'
    )

    assert views.get(PROJECT, VIEW).name == "Review queue"
    patch = ProjectTaskViewUpdate(filter_json=None, columns_json=[])
    assert views.update(PROJECT, VIEW, patch).filter_json == {}
    assert (
        update_route.calls.last.request.read()
        == b'{"filter_json":null,"columns_json":[]}'
    )

    views.delete(PROJECT, VIEW)
    assert delete_route.called

    assert views.copy(PROJECT, VIEW, ProjectTaskViewCopyRequest()).id == UUID(COPY)
    assert copy_route.calls.last.request.read() == b"{}"

    request = ProjectTaskQueryRequest(
        filter_json=dynamic_filter,
        sort_json=[{"field": "task.created_at", "direction": "desc"}],
        columns_json=["display_id", "annotation_count"],
        limit=10,
    )
    result = views.query(PROJECT, request)
    assert result.items[0].annotation_count == 3
    assert result.items[0].model_versions == ["m1"]
    assert query_route.calls.last.request.method == "POST"
    assert (
        query_route.calls.last.request.content.find(b"annotation.attribute.bbox.color")
        >= 0
    )

    assert views.query_view(PROJECT, VIEW, limit=10).total == 1
    assert dict(query_view_route.calls.last.request.url.params) == {
        "limit": "10",
        "offset": "0",
    }


def test_query_and_patch_models_preserve_dynamic_json_and_explicit_empty_values():
    filter_json = {
        "field": "annotation.attribute.unit.dynamic_field",
        "op": "eq",
        "value": {"v": 2},
    }
    query = ProjectTaskQueryRequest(filter_json=filter_json, columns_json=[])
    assert query.model_dump(mode="json", exclude_unset=True) == {
        "filter_json": filter_json,
        "columns_json": [],
    }

    patch = ProjectTaskViewUpdate(filter_json=None, sort_json=[], columns_json=[])
    assert patch.model_dump(mode="json", exclude_unset=True) == {
        "filter_json": None,
        "sort_json": [],
        "columns_json": [],
    }
    assert ProjectTaskViewCopyRequest().model_dump(exclude_unset=True) == {}


@pytest.mark.parametrize("limit", [0, 201, True])
def test_query_request_rejects_invalid_limit(limit):
    with pytest.raises(ValidationError):
        ProjectTaskQueryRequest(limit=limit)


@pytest.mark.parametrize("offset", [-1])
def test_query_request_rejects_invalid_offset(offset):
    with pytest.raises(ValidationError):
        ProjectTaskQueryRequest(offset=offset)


def test_query_view_reuses_query_request_bounds(client, respx_mock):
    views = TaskViews(client._http)
    route = respx_mock.get(f"{API}/projects/{PROJECT}/task-views/{VIEW}/tasks")
    with pytest.raises(ValidationError):
        views.query_view(PROJECT, VIEW, limit=0)
    assert not route.called
