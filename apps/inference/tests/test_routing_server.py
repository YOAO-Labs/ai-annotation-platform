from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from aap_inference_contract.routing import (
    ROUTING_REPLY_MAX_BYTES,
    ROUTING_REQUEST_MAX_BYTES,
    ROUTING_RESULT_PROTOCOL,
    ROUTING_RUNTIME_RESULT_PROTOCOL,
    RoutingResult,
    RoutingRuntimeResult,
    decode_routing_result,
    decode_routing_runtime_result,
    make_routing_command,
    make_routing_runtime_request,
    make_routing_status_request,
    routing_command_bytes,
    routing_command_sha256,
    routing_runtime_request_bytes,
    routing_status_request_bytes,
)

from aap_inference.pipeline_server import PipelineHttpServer

_REGISTRY_ID = UUID("22222222-2222-4222-8222-222222222222")


class _RoutingManager:
    def __init__(self) -> None:
        self.commands = []
        self.statuses = []
        self.runtimes = []

    async def execute(self, command):
        self.commands.append(command)
        return RoutingResult(
            schema=ROUTING_RESULT_PROTOCOL,
            request_id=command.request_id,
            command_sha256=routing_command_sha256(command),
            operation=command.operation,
            state="committed",
            result={"kind": "rejected", "reason": "pool_unavailable"},
        )

    async def status(self, request):
        self.statuses.append(request)
        return RoutingResult(
            schema=ROUTING_RESULT_PROTOCOL,
            request_id=request.request_id,
            command_sha256=request.command_sha256,
            operation=request.operation,
            state="unknown",
            result=None,
        )

    async def runtime(self, request):
        self.runtimes.append(request)
        return RoutingRuntimeResult(
            schema=ROUTING_RUNTIME_RESULT_PROTOCOL,
            request_id=request.request_id,
            observed_at=datetime(2026, 10, 22, 2, 30, tzinfo=UTC),
            registries=[
                {
                    "registry_id": _REGISTRY_ID,
                    "open_scopes": 2,
                    "active_attempts": 1,
                    "uncertain_attempts": 1,
                    "continuity": "ready",
                    "authority_epoch": UUID("33333333-3333-4333-8333-333333333333"),
                    "backend_incarnation": None,
                    "control_id": None,
                }
            ],
        )


def _command():
    return make_routing_command(
        "acquire",
        {
            "pool_id": None,
            "registry_id": str(_REGISTRY_ID),
            "project_id": None,
            "mode": "direct",
            "required_instance_id": str(_REGISTRY_ID),
            "owner": "routing-server-test",
            "operation": "predict",
        },
    )


@pytest.mark.asyncio
async def test_route_command_is_authenticated_and_returns_bound_typed_result():
    manager = _RoutingManager()
    server = PipelineHttpServer(
        None,
        None,
        None,
        routing_manager=manager,
        routing_control_token="separate-route-token",
    )
    command = _command()

    status, encoded = await server._dispatch(
        "POST",
        "/v1/routes/command",
        {"authorization": "Bearer separate-route-token"},
        routing_command_bytes(command),
    )

    assert status == 200
    assert len(encoded) <= ROUTING_REPLY_MAX_BYTES
    reply = decode_routing_result(command, encoded)
    assert reply.state == "committed"
    assert reply.result.reason == "pool_unavailable"
    assert manager.commands == [command]


@pytest.mark.asyncio
async def test_route_status_preserves_bound_unknown_receipt():
    manager = _RoutingManager()
    server = PipelineHttpServer(
        None,
        None,
        None,
        routing_manager=manager,
        routing_control_token="separate-route-token",
    )
    command = _command()
    request = make_routing_status_request(command)

    status, encoded = await server._dispatch(
        "POST",
        "/v1/routes/status",
        {"authorization": "Bearer separate-route-token"},
        routing_status_request_bytes(request),
    )

    assert status == 200
    result = json.loads(encoded)
    assert result["state"] == "unknown"
    assert result["result"] is None
    assert result["operation"] == "acquire"
    assert manager.statuses == [request]


@pytest.mark.asyncio
async def test_route_endpoint_rejects_bad_auth_and_duplicate_json_without_dispatch():
    manager = _RoutingManager()
    server = PipelineHttpServer(
        None,
        None,
        None,
        routing_manager=manager,
        routing_control_token="separate-route-token",
    )
    command = _command()
    encoded = routing_command_bytes(command).decode("utf-8")
    duplicate_body = encoded.replace(
        '"operation":"acquire"',
        '"operation":"acquire","operation":"acquire"',
        1,
    ).encode("utf-8")

    bad_auth_status, _ = await server._dispatch(
        "POST", "/v1/routes/command", {"authorization": "Bearer wrong"}, duplicate_body
    )
    malformed_status, _ = await server._dispatch(
        "POST",
        "/v1/routes/command",
        {"authorization": "Bearer separate-route-token"},
        duplicate_body,
    )

    assert bad_auth_status == 401
    assert malformed_status == 400
    assert manager.commands == []


def test_route_server_rejects_a_token_shared_with_an_existing_owner():
    with pytest.raises(ValueError):
        PipelineHttpServer(
            None,
            None,
            "pipeline-token",
            routing_manager=_RoutingManager(),
            routing_control_token="pipeline-token",
        )


def test_route_paths_use_the_wire_body_budget():
    server = PipelineHttpServer(
        None,
        None,
        None,
        routing_manager=_RoutingManager(),
        routing_control_token="separate-route-token",
    )

    assert server._request_limit("/v1/routes/command") == ROUTING_REQUEST_MAX_BYTES
    assert (
        server._request_limit("/v1/routes/status?unsupported=1")
        == ROUTING_REQUEST_MAX_BYTES
    )
    assert server._request_limit("/v1/routes/runtime") == ROUTING_REQUEST_MAX_BYTES


@pytest.mark.asyncio
async def test_runtime_snapshot_is_read_only_authenticated_and_bound():
    manager = _RoutingManager()
    server = PipelineHttpServer(
        None,
        None,
        None,
        routing_manager=manager,
        routing_control_token="separate-route-token",
    )
    request = make_routing_runtime_request([_REGISTRY_ID])

    status, encoded = await server._dispatch(
        "POST",
        "/v1/routes/runtime",
        {"authorization": "Bearer separate-route-token"},
        routing_runtime_request_bytes(request),
    )

    assert status == 200
    reply = decode_routing_runtime_result(request, encoded)
    assert reply.registries[0].open_scopes == 2
    assert manager.runtimes == [request]
    assert manager.commands == []
    assert manager.statuses == []


@pytest.mark.asyncio
async def test_runtime_snapshot_rejects_duplicate_registry_ids():
    manager = _RoutingManager()
    server = PipelineHttpServer(
        None,
        None,
        None,
        routing_manager=manager,
        routing_control_token="separate-route-token",
    )
    request = make_routing_runtime_request([_REGISTRY_ID])
    body = routing_runtime_request_bytes(request).decode("utf-8")
    body = body.replace(
        f'"registry_ids":["{_REGISTRY_ID}"]',
        f'"registry_ids":["{_REGISTRY_ID}","{_REGISTRY_ID}"]',
    ).encode("utf-8")

    status, _ = await server._dispatch(
        "POST",
        "/v1/routes/runtime",
        {"authorization": "Bearer separate-route-token"},
        body,
    )

    assert status == 400
    assert manager.runtimes == []
