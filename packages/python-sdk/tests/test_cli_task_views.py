"""CLI contracts for project saved views and read-only task queries."""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from ai_annotation import Client
from ai_annotation.cli import task_views
from ai_annotation.task_views import TaskViews

from .conftest import API, BASE

PROJECT = "11111111-1111-4111-8111-111111111111"
VIEW = "22222222-2222-4222-8222-222222222222"
ENV = {
    "AAP_BASE_URL": BASE,
    "AAP_API_KEY": "ak_test",
    "NO_COLOR": "1",
    "TERM": "dumb",
    "COLUMNS": "120",
}


@pytest.fixture
def cli_client_factory(monkeypatch):
    clients = []

    def make_client(*_args, **_kwargs):
        client = Client(base_url=BASE, api_key="ak_test")
        client.task_views = TaskViews(client._http)
        clients.append(client)
        return client

    monkeypatch.setattr(task_views, "get_client", make_client)
    return clients


def _view_response(view_id=VIEW):
    return {
        "id": view_id,
        "key": None,
        "project_id": PROJECT,
        "owner_id": PROJECT,
        "name": "Private queue",
        "visibility": "private",
        "entity_scope": "tasks",
        "filter_json": {},
        "sort_json": [],
        "columns_json": [],
        "builtin": False,
        "task_count": 2,
        "result_count": 2,
        "created_at": "2026-10-17T00:00:00Z",
        "updated_at": "2026-10-17T00:00:00Z",
        "invalid_fields": [],
    }


def test_delete_requires_confirmation_before_client_or_request(
    cli_client_factory, respx_mock
):
    route = respx_mock.delete(f"{API}/projects/{PROJECT}/task-views/{VIEW}").mock(
        return_value=httpx.Response(204)
    )
    result = CliRunner().invoke(
        task_views.app,
        ["delete", VIEW, "--project", PROJECT, "--json"],
        env=ENV,
    )
    assert result.exit_code == 2
    assert "--yes" in result.output
    assert not route.called
    assert cli_client_factory == []


def test_list_rejects_unknown_scope_before_client(cli_client_factory):
    result = CliRunner().invoke(
        task_views.app,
        ["list", "--project", PROJECT, "--scope", "unknown", "--json"],
        env=ENV,
    )
    assert result.exit_code == 2
    assert "tasks、objects 或 tracks" in result.output
    assert cli_client_factory == []


def test_delete_with_yes_emits_json(cli_client_factory, respx_mock):
    route = respx_mock.delete(f"{API}/projects/{PROJECT}/task-views/{VIEW}").mock(
        return_value=httpx.Response(204)
    )
    result = CliRunner().invoke(
        task_views.app,
        ["delete", VIEW, "--project", PROJECT, "--yes", "--json"],
        env=ENV,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "deleted": True,
        "project_id": PROJECT,
        "view_id": VIEW,
    }
    assert route.called
    assert len(cli_client_factory) == 1


def test_copy_reads_required_body_from_stdin(cli_client_factory, respx_mock):
    route = respx_mock.post(f"{API}/projects/{PROJECT}/task-views/{VIEW}/copy").mock(
        return_value=httpx.Response(201, json=_view_response())
    )
    result = CliRunner().invoke(
        task_views.app,
        ["copy", VIEW, "--project", PROJECT, "--body", "-", "--json"],
        input='{"visibility":"private"}',
        env=ENV,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["id"] == VIEW
    assert json.loads(route.calls.last.request.content) == {"visibility": "private"}
    assert len(cli_client_factory) == 1


def test_task_view_query_is_read_only_post_and_accepts_dynamic_filter_from_stdin(
    cli_client_factory, respx_mock
):
    route = respx_mock.post(f"{API}/projects/{PROJECT}/tasks/query").mock(
        return_value=httpx.Response(
            200,
            json={"items": [], "total": 0, "limit": 50, "offset": 0},
        )
    )
    result = CliRunner().invoke(
        task_views.app,
        ["query", "--project", PROJECT, "--body", "-", "--json"],
        input=json.dumps(
            {
                "filter_json": {
                    "field": "annotation.attribute.unit.dynamic_key",
                    "op": "eq",
                    "value": "x",
                },
                "columns_json": [],
            }
        ),
        env=ENV,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["total"] == 0
    assert route.calls.last.request.method == "POST"
    assert b"dynamic_key" in route.calls.last.request.content
    assert len(cli_client_factory) == 1


def test_saved_view_tasks_alias_forwards_offset_query_params(
    cli_client_factory, respx_mock
):
    route = respx_mock.get(f"{API}/projects/{PROJECT}/task-views/{VIEW}/tasks").mock(
        return_value=httpx.Response(
            200,
            json={"items": [], "total": 7, "limit": 3, "offset": 6},
        )
    )
    result = CliRunner().invoke(
        task_views.app,
        [
            "tasks",
            VIEW,
            "--project",
            PROJECT,
            "--limit",
            "3",
            "--offset",
            "6",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["offset"] == 6
    assert dict(route.calls.last.request.url.params) == {"limit": "3", "offset": "6"}
    assert len(cli_client_factory) == 1
