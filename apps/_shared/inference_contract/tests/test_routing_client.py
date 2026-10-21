"""Single-command HTTP recovery checks for the routing owner client."""

from __future__ import annotations

import httpx
import pytest
import uuid

from aap_inference_contract.routing import (
    ROUTING_RESULT_PROTOCOL,
    RoutingResult,
    RoutingUnavailable,
    decode_routing_command,
    decode_routing_status_request,
    make_routing_command,
    make_routing_status_request,
    routing_command_sha256,
    routing_result_bytes,
)
from aap_inference_contract.routing_client import RoutingHttpClient

_REQUEST_ID = uuid.UUID("9c4097c8-2b9a-4191-bb48-44b247465172")
_SCOPE_ID = uuid.UUID("560f02f7-1378-4708-b3a7-6992047c1fb6")
_REGISTRY_ID = uuid.UUID("c864b2a8-83f3-4f6a-bb40-f4ef8f0512bf")
_EPOCH_ID = uuid.UUID("d642cc73-96db-45ca-aec3-b0b2b834ee95")
_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


def _command():
    return make_routing_command(
        "acquire",
        {
            "pool_id": None,
            "registry_id": _REGISTRY_ID,
            "project_id": None,
            "mode": "direct",
            "required_instance_id": _REGISTRY_ID,
            "owner": "model-market",
            "operation": "model-trial",
        },
        request_id=_REQUEST_ID,
    )


def _result(command, *, state="committed", result=None, operation=None):
    return RoutingResult.model_validate(
        {
            "schema": ROUTING_RESULT_PROTOCOL,
            "request_id": command.request_id,
            "command_sha256": routing_command_sha256(command),
            "operation": operation or command.operation,
            "state": state,
            "result": result
            if result is not None or state == "unknown"
            else {
                "id": _SCOPE_ID,
                "pool_id": None,
                "registry_id": _REGISTRY_ID,
                "project_id": None,
                "mode": "direct",
                "owner": "model-market",
                "operation": "model-trial",
                "generation": None,
                "config_revision": 1,
                "eligibility_revision": 1,
                "authority_epoch": _EPOCH_ID,
                "state": "reserved",
                "closed": False,
                "heartbeat_deadline_ms": 1782090000000,
                "capability": "opaque-scope-token",
            },
        }
    )


def _install_mock_transport(
    monkeypatch, transport: httpx.AsyncBaseTransport
) -> list[dict]:
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
async def test_execute_posts_one_command_with_bearer_and_bound_result(
    monkeypatch,
) -> None:
    requests = []
    command = _command()

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        typed = decode_routing_command(await request.aread())
        assert typed == command
        return httpx.Response(
            200,
            content=routing_result_bytes(_result(typed)),
            headers={"content-type": "application/json"},
        )

    options = _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    client = RoutingHttpClient(
        "http://routing-owner.local/base/", "private-routing-token"
    )
    result = await client.execute(command)

    assert result.state == "committed"
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/base/v1/routes/command"
    assert requests[0].headers["authorization"] == "Bearer private-routing-token"
    assert "private-routing-token" not in (await requests[0].aread()).decode("utf-8")
    assert options == [{"follow_redirects": False, "trust_env": False}]


@pytest.mark.asyncio
async def test_lost_ack_recovers_once_from_bound_status_without_command_resubmit(
    monkeypatch,
) -> None:
    command = _command()
    paths = []

    async def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/command"):
            raise httpx.ReadError("ack lost after commit", request=request)
        status = decode_routing_status_request(await request.aread())
        assert status.request_id == command.request_id
        assert status.command_sha256 == routing_command_sha256(command)
        assert status.operation == command.operation
        return httpx.Response(
            200,
            content=routing_result_bytes(_result(command)),
            headers={"content-type": "application/json"},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    result = await RoutingHttpClient("http://routing-owner.local", "token").execute(
        command
    )

    assert result.state == "committed"
    assert paths == ["/v1/routes/command", "/v1/routes/status"]
    assert paths.count("/v1/routes/command") == 1


@pytest.mark.asyncio
async def test_pending_or_malformed_ack_uses_one_status_and_never_reports_unknown_as_success(
    monkeypatch,
) -> None:
    command = _command()
    paths = []

    async def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/command"):
            return httpx.Response(
                200,
                content=routing_result_bytes(_result(command, state="caller_pending")),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            200,
            content=routing_result_bytes(
                _result(command, state="unknown", result=None)
            ),
            headers={"content-type": "application/json"},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    with pytest.raises(RoutingUnavailable):
        await RoutingHttpClient("http://routing-owner.local", "token").execute(command)
    assert paths == ["/v1/routes/command", "/v1/routes/status"]


@pytest.mark.asyncio
async def test_malformed_command_ack_recovers_one_status_once(monkeypatch) -> None:
    command = _command()
    paths = []

    async def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/command"):
            return httpx.Response(200, json={"schema": ROUTING_RESULT_PROTOCOL})
        return httpx.Response(
            200,
            content=routing_result_bytes(_result(command)),
            headers={"content-type": "application/json"},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    result = await RoutingHttpClient("http://routing-owner.local", "token").execute(
        command
    )
    assert result.state == "committed"
    assert paths == ["/v1/routes/command", "/v1/routes/status"]


@pytest.mark.asyncio
async def test_status_rejects_compressed_response_without_retry(monkeypatch) -> None:
    calls = 0
    request = make_routing_status_request(_command())

    async def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=b"{}",
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
            },
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    with pytest.raises(RoutingUnavailable):
        await RoutingHttpClient("http://routing-owner.local", "token").status(request)
    assert calls == 1


def test_client_rejects_invalid_provider_configuration() -> None:
    for base_url, token, timeout in (
        ("file:///tmp/routing", "token", 30),
        ("http://user:password@owner.local", "token", 30),
        ("http://owner.local", "", 30),
        ("http://owner.local", "token", True),
    ):
        with pytest.raises(RoutingUnavailable):
            RoutingHttpClient(base_url, token, timeout_seconds=timeout)
