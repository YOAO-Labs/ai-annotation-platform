from __future__ import annotations

import json

import httpx
import pytest

from aap_inference.scope_retirement_transport import (
    BackendScopeRetirementUnavailable,
    execute_backend_scope_retirement,
)

_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


def _context():
    return {
        "retirement_id": "e469fa57-ff59-42ae-9fe7-366f895931f7",
        "scope_id": "c864b2a8-83f3-4f6a-bb40-f4ef8f0512bf",
        "registry_id": "d4e6676d-9fe4-4c33-9ca5-fc1c31230052",
        "authority_epoch": "6a7a2139-2428-4c28-b4c8-6b0c40034289",
        "backend_incarnation": "be189eb2-2e7d-4c36-94b0-20ad78d68147",
        "challenge": "a" * 64,
        "fence_until": 1_800_000_000,
        "endpoint": {
            "url": "http://backend.local/base/",
            "auth_method": "token",
            "auth_token": "private-management-token",
        },
    }


def _reply():
    context = _context()
    return {
        "schema": "aap-backend-scope-retirement-reply/v1",
        "retirement_id": context["retirement_id"],
        "scope_id": context["scope_id"],
        "registry_id": context["registry_id"],
        "authority_epoch": context["authority_epoch"],
        "backend_incarnation": context["backend_incarnation"],
        "challenge": context["challenge"],
        "phase": "retired",
        "active_requests": 0,
    }


def _install_transport(monkeypatch, transport):
    options = []

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        options.append(
            {
                "follow_redirects": kwargs.get("follow_redirects"),
                "trust_env": kwargs.get("trust_env"),
            }
        )
        return _HTTPX_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    return options


@pytest.mark.asyncio
async def test_backend_retirement_uses_private_bearer_and_exact_selected_endpoint(
    monkeypatch,
):
    requests = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/base/v1/aap-routing/retire-scope"
        assert request.headers["authorization"] == "Bearer private-management-token"
        command = json.loads(await request.aread())
        assert command["schema"] == "aap-backend-scope-retirement/v1"
        assert "private-management-token" not in json.dumps(command)
        return httpx.Response(
            200,
            content=json.dumps(_reply()).encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    options = _install_transport(monkeypatch, httpx.MockTransport(handle))
    reply = await execute_backend_scope_retirement(_context())

    assert reply.as_dict() == _reply()
    assert len(requests) == 1
    assert options == [{"follow_redirects": False, "trust_env": False}]


@pytest.mark.asyncio
async def test_backend_retirement_rejects_compressed_or_oversized_reply_once(
    monkeypatch,
):
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=json.dumps(_reply()).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
            },
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))
    with pytest.raises(BackendScopeRetirementUnavailable):
        await execute_backend_scope_retirement(_context())
    assert calls == 1
