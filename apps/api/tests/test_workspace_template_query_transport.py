"""Reader selection and failure mapping without a database or local fallback."""

from __future__ import annotations

import json
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import httpx
import pytest

from aap_workspace.template_query_contract import (
    decode_template_query_request,
    template_query_success_reply,
)
from app.config import settings
from app.services import workspace_template_query as commands


@pytest.fixture(autouse=True)
def selectors(monkeypatch):
    for name in (
        "workspace_template_query_url",
        "workspace_template_query_database_url",
        "workspace_template_query_database_url_file",
        "workspace_membership_control_token_file",
    ):
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "workspace_membership_control_token", "private-token")


async def _list():
    return await commands.execute_template_query(
        object(),
        operation="list_templates",
        actor=SimpleNamespace(id=uuid.uuid4(), role="project_admin"),
        filters={"scope": None, "type_key": None, "search": None},
    )


def _forbid_local(_data):
    raise AssertionError("Selected remote reader must not use a local data port")


async def test_http_reader_binds_reply_and_keeps_admitted_actor(monkeypatch):
    seen = []
    client_type = httpx.AsyncClient

    async def handle(request):
        command = decode_template_query_request(json.loads(request.content))
        seen.append(command)
        assert command.admitted_actor.role == "project_admin"
        assert request.headers["authorization"] == "Bearer private-token"
        reply = template_query_success_reply(command, "[]")
        return httpx.Response(200, json=reply.model_dump(mode="json", by_alias=True))

    monkeypatch.setattr(settings, "workspace_template_query_url", "http://owner")
    monkeypatch.setattr(commands, "TemplateQueryManager", _forbid_local)
    monkeypatch.setattr(
        commands.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs),
    )
    assert await _list() == []
    assert len(seen) == 1


@pytest.mark.parametrize("status", [400, 500, 503])
async def test_remote_status_does_not_fall_back_or_hide_internal_error(
    monkeypatch, status
):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(settings, "workspace_template_query_url", "http://owner")
    monkeypatch.setattr(commands, "TemplateQueryManager", _forbid_local)
    monkeypatch.setattr(
        commands.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(lambda _: httpx.Response(status)), **kwargs
        ),
    )
    expected = (
        commands.TemplateQueryOwnerInternalError if status == 500 else HTTPException
    )
    with pytest.raises(expected) as failure:
        await _list()
    if status != 500:
        assert failure.value.status_code == 503


async def test_url_and_dsn_conflict_rejects_without_opening_a_reader(monkeypatch):
    monkeypatch.setattr(settings, "workspace_template_query_url", "http://owner")
    monkeypatch.setattr(
        settings, "workspace_template_query_database_url", "postgresql://unused"
    )
    monkeypatch.setattr(commands, "TemplateQueryManager", _forbid_local)
    with pytest.raises(HTTPException) as failure:
        await _list()
    assert failure.value.status_code == 503
