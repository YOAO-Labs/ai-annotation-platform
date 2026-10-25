from __future__ import annotations

import base64
import json

import httpx
import pytest
from aap_protocol_v2.routing import (
    ROUTE_CONTROL_PATH,
    ROUTE_CONTROL_REPLY_PROTOCOL,
    ROUTE_STATUS_PATH,
)

from aap_inference.routing_backend_control import (
    BackendRoutingControl,
    BackendRoutingControlUnavailable,
)

CONTROL_ID = "11111111-1111-4111-8111-111111111111"
REGISTRY_ID = "22222222-2222-4222-8222-222222222222"
NEW_EPOCH = "33333333-3333-4333-8333-333333333333"
INCARNATION = "44444444-4444-4444-8444-444444444444"
RESTARTED_INCARNATION = "55555555-5555-4555-8555-555555555555"
CHALLENGE = "a" * 64
SIGNING_KEY = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode("ascii")


def _context(**endpoint_overrides):
    endpoint = {
        "url": "https://backend.example.test/base/",
        "auth_method": "token",
        "auth_token": "registry-token",
    }
    endpoint.update(endpoint_overrides)
    return {
        "control_id": CONTROL_ID,
        "registry_id": REGISTRY_ID,
        "phase": "draining",
        "new_epoch": NEW_EPOCH,
        "challenge": CHALLENGE,
        "retired_epoch": None,
        "backend_incarnation": None,
        "endpoint": endpoint,
        "signing_key": SIGNING_KEY,
    }


def _reply(*, phase: str, active_requests: int) -> dict[str, object]:
    return {
        "schema": ROUTE_CONTROL_REPLY_PROTOCOL,
        "command_id": CONTROL_ID,
        "registry_id": REGISTRY_ID,
        "authority_epoch": NEW_EPOCH,
        "backend_incarnation": INCARNATION,
        "challenge": CHALLENGE,
        "phase": phase,
        "active_requests": active_requests,
    }


@pytest.mark.asyncio
async def test_active_drain_returns_progress_without_sending_bind():
    actions = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/base{ROUTE_CONTROL_PATH}"
        assert request.headers["authorization"] == "Bearer registry-token"
        command = json.loads(request.content)
        actions.append(command["action"])
        assert command["signing_key"] is None
        return httpx.Response(200, json=_reply(phase="draining", active_requests=2))

    control = BackendRoutingControl(
        transport_factory=lambda: httpx.MockTransport(respond)
    )
    reply = await control(_context())

    assert reply["phase"] == "draining"
    assert reply["active_requests"] == 2
    assert actions == ["drain"]


@pytest.mark.asyncio
async def test_zero_active_drain_sends_one_bind_with_private_key():
    commands = []

    def respond(request: httpx.Request) -> httpx.Response:
        command = json.loads(request.content)
        commands.append(command)
        if command["action"] == "drain":
            return httpx.Response(200, json=_reply(phase="drained", active_requests=0))
        return httpx.Response(200, json=_reply(phase="bound", active_requests=0))

    control = BackendRoutingControl(
        transport_factory=lambda: httpx.MockTransport(respond)
    )
    reply = await control(_context())

    assert reply["phase"] == "bound"
    assert [command["action"] for command in commands] == ["drain", "bind"]
    assert commands[0]["signing_key"] is None
    assert commands[1]["signing_key"] == SIGNING_KEY


@pytest.mark.asyncio
async def test_ambiguous_drain_is_not_retried():
    calls = 0

    def lose_reply(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("ambiguous response")

    control = BackendRoutingControl(
        transport_factory=lambda: httpx.MockTransport(lose_reply)
    )

    with pytest.raises(BackendRoutingControlUnavailable):
        await control(_context())

    assert calls == 1


@pytest.mark.asyncio
async def test_duplicate_reply_fields_are_rejected():
    calls = 0

    def duplicate_reply(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.dumps(_reply(phase="drained", active_requests=0))
        body = body.replace('"schema":', '"schema":"duplicate","schema":', 1)
        return httpx.Response(200, content=body)

    control = BackendRoutingControl(
        transport_factory=lambda: httpx.MockTransport(duplicate_reply)
    )

    with pytest.raises(BackendRoutingControlUnavailable):
        await control(_context())

    assert calls == 1


@pytest.mark.asyncio
async def test_non_token_endpoint_fails_before_http():
    calls = 0

    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    control = BackendRoutingControl(
        transport_factory=lambda: httpx.MockTransport(unexpected_request)
    )
    context = _context(auth_method="none", auth_token=None)

    with pytest.raises(BackendRoutingControlUnavailable):
        await control(context)

    assert calls == 0


@pytest.mark.asyncio
async def test_endpoint_userinfo_is_rejected_before_http():
    calls = 0

    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    control = BackendRoutingControl(
        transport_factory=lambda: httpx.MockTransport(unexpected_request)
    )

    with pytest.raises(BackendRoutingControlUnavailable):
        await control(_context(url="https://user:password@backend.example.test"))

    assert calls == 0


@pytest.mark.asyncio
async def test_authenticated_status_get_is_bound_to_frozen_incarnation():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/base{ROUTE_STATUS_PATH}"
        assert request.url.query == b""
        assert request.headers["authorization"] == "Bearer registry-token"
        assert request.headers["accept-encoding"] == "identity"
        assert request.content == b""
        return httpx.Response(200, json=_reply(phase="bound", active_requests=0))

    context = _context()
    context["backend_incarnation"] = INCARNATION
    control = BackendRoutingControl(
        transport_factory=lambda: httpx.MockTransport(respond)
    )

    reply = await control.status(context)

    assert reply["backend_incarnation"] == INCARNATION
    assert reply["registry_id"] == REGISTRY_ID


@pytest.mark.asyncio
async def test_status_rejects_a_different_frozen_incarnation():
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                **_reply(phase="bound", active_requests=0),
                "backend_incarnation": RESTARTED_INCARNATION,
            },
        )

    context = _context()
    context["backend_incarnation"] = INCARNATION
    control = BackendRoutingControl(
        transport_factory=lambda: httpx.MockTransport(respond)
    )

    with pytest.raises(BackendRoutingControlUnavailable):
        await control.status(context)
