"""Selected Project owners bind replies and preserve public failure phases."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import httpx
import pytest

from aap_workspace_contract.project_query_contract import (
    PROJECT_QUERY_PROTOCOL,
    ProjectQuerySuccess,
    decode_project_query_request,
)
from app.config import settings
from app.services import workspace_project_query as queries


@pytest.fixture(autouse=True)
def selectors(monkeypatch):
    for name in (
        "workspace_project_query_url",
        "workspace_project_query_database_url",
        "workspace_project_query_database_url_file",
        "workspace_membership_control_token_file",
    ):
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "workspace_membership_control_token", "test-token")


async def _list():
    return await queries.execute_project_query(
        object(),
        operation="list_projects",
        actor=SimpleNamespace(id=uuid.UUID(int=1), role="project_admin"),
        filters=dict.fromkeys(
            (
                "status",
                "search",
                "type_key",
                "data_type",
                "member_id",
                "project_role",
                "created_from",
                "created_to",
            )
        ),
    )


def _select_http(monkeypatch, handler):
    client_type = httpx.AsyncClient

    def forbidden(*args, **kwargs):
        raise AssertionError("A selected owner must not fall back to a local reader")

    monkeypatch.setattr(settings, "workspace_project_query_url", "http://workspace")
    monkeypatch.setattr(queries, "ProjectQueryManager", forbidden)
    monkeypatch.setattr(
        queries.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )


async def test_http_owner_preserves_admission_and_validates_the_bound_reply(
    monkeypatch,
):
    requests = []

    def handle(request):
        command = decode_project_query_request(request.content)
        requests.append(command)
        assert request.url.path == "/v1/workspace/project-queries/execute"
        assert request.headers["authorization"] == "Bearer test-token"
        assert command.admitted_actor.role == "project_admin"
        reply = ProjectQuerySuccess(
            schema=PROJECT_QUERY_PROTOCOL, ok=True, request=command, data_json="[]"
        )
        return httpx.Response(200, json=reply.model_dump(mode="json", by_alias=True))

    _select_http(monkeypatch, handle)
    assert await _list() == []
    assert len(requests) == 1


@pytest.mark.parametrize("status", [400, 500, 503])
async def test_remote_status_preserves_plain_internal_error_without_fallback(
    monkeypatch, status
):
    _select_http(monkeypatch, lambda _: httpx.Response(status))
    expected = (
        queries.ProjectQueryOwnerInternalError if status == 500 else HTTPException
    )
    with pytest.raises(expected) as failure:
        await _list()
    if status != 500:
        assert failure.value.status_code == 503
        assert failure.value.detail == "项目查询服务暂不可用"


async def test_profile_refusal_keeps_its_specific_public_detail(monkeypatch):
    def handle(request):
        command = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "schema": PROJECT_QUERY_PROTOCOL,
                "ok": False,
                "request": command,
                "error": {
                    "status_code": 503,
                    "detail": "身份资料服务暂不可用",
                    "headers": None,
                },
            },
        )

    _select_http(monkeypatch, handle)
    with pytest.raises(HTTPException) as failure:
        await _list()
    assert failure.value.status_code == 503
    assert failure.value.detail == "身份资料服务暂不可用"


async def test_success_for_another_request_is_unavailable(monkeypatch):
    def handle(request):
        command = json.loads(request.content)
        command["admitted_actor"]["id"] = str(uuid.UUID(int=2))
        return httpx.Response(
            200,
            json={
                "schema": PROJECT_QUERY_PROTOCOL,
                "ok": True,
                "request": command,
                "data_json": "[]",
            },
        )

    _select_http(monkeypatch, handle)
    with pytest.raises(HTTPException) as failure:
        await _list()
    assert failure.value.status_code == 503


async def test_http_deadline_never_retries_locally(monkeypatch):
    calls = []

    async def handle(request):
        calls.append(request)
        await asyncio.Event().wait()

    _select_http(monkeypatch, handle)
    monkeypatch.setattr(settings, "workspace_project_query_timeout_seconds", 0.01)
    with pytest.raises(HTTPException) as failure:
        await asyncio.wait_for(_list(), timeout=1)
    assert failure.value.status_code == 503 and len(calls) == 1


async def test_conflicting_provider_selection_fails_before_io(monkeypatch):
    def forbidden(_request):
        raise AssertionError("Conflicting configuration must not make a request")

    _select_http(monkeypatch, forbidden)
    monkeypatch.setattr(
        settings, "workspace_project_query_database_url", "postgresql://unused"
    )
    with pytest.raises(HTTPException) as failure:
        await _list()
    assert failure.value.status_code == 503
