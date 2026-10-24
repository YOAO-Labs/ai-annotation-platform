from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.middleware.audit import AuditMiddleware, _build_payload


def _request(*, state: dict, authorization: str | None = None) -> Request:
    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/example",
            "raw_path": b"/api/v1/example",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
            "state": state,
        }
    )


def test_audit_uses_verified_identity_principal():
    actor_id = uuid.uuid4()
    request = _request(
        state={
            "identity_principal": SimpleNamespace(actor_id=actor_id, role="employee")
        },
        authorization="Bearer stale-token-claim",
    )

    payload = _build_payload(request, 200)

    assert payload["actor_id"] == str(actor_id)
    assert payload["actor_role"] == "employee"


def test_audit_does_not_infer_actor_from_bearer_claims():
    request = _request(state={}, authorization="Bearer not-a-verified-token")

    payload = _build_payload(request, 401)

    assert payload["actor_id"] is None
    assert payload["actor_role"] is None


@pytest.mark.asyncio
async def test_metadata_write_failure_does_not_change_http_response(monkeypatch):
    from app.middleware import audit as audit_module

    request = _request(state={})
    response = Response("committed", status_code=201)
    persist = AsyncMock(side_effect=RuntimeError("audit store unavailable"))
    monkeypatch.setattr(audit_module.settings, "audit_async", False)
    monkeypatch.setattr(audit_module, "_persist_audit_sync", persist)

    async def call_next(_request):
        return response

    result = await AuditMiddleware(lambda *_args: None).dispatch(request, call_next)

    assert result is response
    persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_audit_sync_cancellation_is_not_swallowed(monkeypatch):
    from app.middleware import audit as audit_module

    request = _request(state={})
    response = Response("committed", status_code=201)
    persist = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(audit_module.settings, "audit_async", False)
    monkeypatch.setattr(audit_module, "_persist_audit_sync", persist)

    async def call_next(_request):
        return response

    with pytest.raises(asyncio.CancelledError):
        await AuditMiddleware(lambda *_args: None).dispatch(request, call_next)

    persist.assert_awaited_once()
