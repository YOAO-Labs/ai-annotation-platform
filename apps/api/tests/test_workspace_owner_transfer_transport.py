"""A selected remote binding retries only transport loss, retaining operation identity."""

import json
import uuid

from fastapi import HTTPException
import httpx
import pytest

from app.config import settings
from app.services import workspace_owner_transfer as commands


def _remote(monkeypatch, handler):
    monkeypatch.setattr(settings, "workspace_owner_transfer_url", "http://owner")
    monkeypatch.setattr(
        settings, "workspace_membership_control_token", "private-control"
    )
    monkeypatch.setattr(settings, "workspace_membership_control_token_file", "")
    monkeypatch.setattr(
        settings,
        "workspace_owner_transfer_database_url",
        "postgresql://unused-fallback",
    )
    client = httpx.AsyncClient
    monkeypatch.setattr(
        commands.httpx,
        "AsyncClient",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_lost_response_retries_the_identical_private_operation(monkeypatch):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.headers["authorization"] == "Bearer private-control"
        if len(calls) == 1:
            raise httpx.ReadError("lost response", request=request)
        return httpx.Response(
            200,
            json={
                **body,
                "ok": True,
                "data": {
                    "previous_owner_id": None,
                    "changed": True,
                    "updated_at": "2026-10-21T00:00:00Z",
                },
            },
        )

    _remote(monkeypatch, handler)
    result = await commands.transfer_project_owner(
        None, actor_id=uuid.uuid4(), project_id=uuid.uuid4(), new_owner_id=uuid.uuid4()
    )
    assert result.data.changed is True
    assert len(calls) == 2 and calls[0] == calls[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,expected",
    [
        (
            200,
            {
                "schema": "aap-workspace-owner-transfer/v1",
                "ok": False,
                "error": {
                    "status_code": 409,
                    "detail": {"reason": "project_busy"},
                    "rollback": False,
                },
            },
            409,
        ),
        (200, {"schema": "aap-workspace-owner-transfer/v1", "ok": 1}, 503),
        (503, {"detail": "unavailable"}, 503),
        (500, {"detail": "database failure"}, 500),
    ],
)
async def test_http_or_business_failures_do_not_retry_or_fall_back(
    monkeypatch, status, body, expected
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)

    _remote(monkeypatch, handler)
    with pytest.raises(HTTPException) as failure:
        await commands.transfer_project_owner(
            None,
            actor_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            new_owner_id=uuid.uuid4(),
        )
    assert failure.value.status_code == expected and len(calls) == 1
