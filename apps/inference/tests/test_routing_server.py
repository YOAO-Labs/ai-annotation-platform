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
    RoutingVideoExecutionProof,
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
from aap_inference_contract.scope_retirement import (
    SCOPE_RETIREMENT_REPLY_MAX_BYTES,
    SCOPE_RETIREMENT_REQUEST_MAX_BYTES,
    SCOPE_RETIREMENT_RESULT_PROTOCOL,
    ScopeRetirementResult,
    ScopeRetirementView,
    decode_scope_retirement_result,
    make_scope_retirement_request,
    scope_retirement_request_bytes,
)
from aap_inference_contract.video_execution import (
    VIDEO_EXECUTION_DATA_PROTOCOL,
    VIDEO_EXECUTION_STATUS_PATH,
    VIDEO_EXECUTION_STATUS_REPLY_MAX_BYTES,
    VIDEO_EXECUTION_STATUS_REQUEST_MAX_BYTES,
    decode_video_execution_status_result,
    video_execution_status_request_bytes,
)

from aap_inference.pipeline_server import PipelineHttpServer

_REGISTRY_ID = UUID("22222222-2222-4222-8222-222222222222")


class _RoutingManager:
    def __init__(self) -> None:
        self.commands = []
        self.statuses = []
        self.runtimes = []
        self.video_statuses = []

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

    async def video_execution_status(self, proof):
        self.video_statuses.append(proof)
        return {
            "schema": VIDEO_EXECUTION_DATA_PROTOCOL,
            "status": "ok",
            "intent": None,
            "scope": None,
        }


class _ScopeRetirementManager:
    def __init__(self):
        self.executed = []
        self.statuses = []

    async def execute(self, request):
        self.executed.append(request)
        return _scope_retirement_result(request, phase="completed")

    async def status(self, request):
        self.statuses.append(request)
        return ScopeRetirementResult(
            schema=SCOPE_RETIREMENT_RESULT_PROTOCOL,
            retirement_id=request.retirement_id,
            scope_id=request.scope_id,
            state="unknown",
            result=None,
        )


def _scope_retirement_request():
    return make_scope_retirement_request(
        "e469fa57-ff59-42ae-9fe7-366f895931f7",
        "c864b2a8-83f3-4f6a-bb40-f4ef8f0512bf",
        "private-capability",
    )


def _scope_retirement_result(request, *, phase):
    return ScopeRetirementResult(
        schema=SCOPE_RETIREMENT_RESULT_PROTOCOL,
        retirement_id=request.retirement_id,
        scope_id=request.scope_id,
        state="committed",
        result=ScopeRetirementView(
            retirement_id=request.retirement_id,
            scope_id=request.scope_id,
            registry_id=_REGISTRY_ID,
            authority_epoch=UUID("33333333-3333-4333-8333-333333333333"),
            backend_incarnation=UUID("44444444-4444-4444-8444-444444444444"),
            phase=phase,
            closed=True,
        ),
    )


def _video_status_proof():
    return RoutingVideoExecutionProof(
        job_id=UUID("10b9f6cc-6424-4b4a-8740-3660cb25db17"),
        run_id=UUID("9f8f00b4-6d2f-4fe0-a75e-a28c4e923714"),
        worker_id=UUID("8f87cb4e-636d-4606-983f-e7397cb954f9"),
        generation=4,
        lease_token=UUID("cb72f60a-61b0-4e76-b355-f8e8b976c4a2"),
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


def test_scope_retirement_endpoints_use_their_bounded_wire_budget():
    manager = _ScopeRetirementManager()
    server = PipelineHttpServer(
        None,
        None,
        None,
        scope_retirement_manager=manager,
        routing_control_token="separate-route-token",
    )

    assert (
        server._request_limit("/v1/routes/retire") == SCOPE_RETIREMENT_REQUEST_MAX_BYTES
    )
    assert (
        server._request_limit("/v1/routes/retirement-status?bad=1")
        == SCOPE_RETIREMENT_REQUEST_MAX_BYTES
    )
    assert (
        server._response_limit("/v1/routes/retire") == SCOPE_RETIREMENT_REPLY_MAX_BYTES
    )


def test_video_execution_status_endpoint_uses_bounded_wire_budget():
    server = PipelineHttpServer(
        None,
        None,
        None,
        routing_manager=_RoutingManager(),
        routing_control_token="separate-route-token",
    )

    assert server._request_limit(f"{VIDEO_EXECUTION_STATUS_PATH}?bad=1") == (
        VIDEO_EXECUTION_STATUS_REQUEST_MAX_BYTES
    )
    assert server._response_limit(VIDEO_EXECUTION_STATUS_PATH) == (
        VIDEO_EXECUTION_STATUS_REPLY_MAX_BYTES
    )


@pytest.mark.asyncio
async def test_video_execution_status_endpoint_is_authenticated_proof_bound_read():
    manager = _RoutingManager()
    server = PipelineHttpServer(
        None,
        None,
        None,
        routing_manager=manager,
        routing_control_token="separate-route-token",
    )
    proof = _video_status_proof()
    body = video_execution_status_request_bytes(proof)

    denied, _ = await server._dispatch(
        "POST", VIDEO_EXECUTION_STATUS_PATH, {"authorization": "Bearer wrong"}, body
    )
    malformed, _ = await server._dispatch(
        "POST",
        VIDEO_EXECUTION_STATUS_PATH,
        {"authorization": "Bearer separate-route-token"},
        body.replace(
            b'"job_id":',
            b'"job_id":"' + str(proof.job_id).encode() + b'","job_id":',
        ),
    )
    status, encoded = await server._dispatch(
        "POST",
        VIDEO_EXECUTION_STATUS_PATH,
        {"authorization": "Bearer separate-route-token"},
        body,
    )

    assert denied == 401
    assert malformed == 400
    assert status == 200
    result = decode_video_execution_status_result(proof, encoded)
    assert result == {"intent": None, "scope": None}
    assert manager.video_statuses == [proof]
    assert manager.commands == []
    assert manager.statuses == []


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


@pytest.mark.asyncio
async def test_retire_endpoint_is_authenticated_and_returns_bound_result():
    manager = _ScopeRetirementManager()
    server = PipelineHttpServer(
        None,
        None,
        None,
        scope_retirement_manager=manager,
        routing_control_token="separate-route-token",
    )
    request = _scope_retirement_request()
    body = scope_retirement_request_bytes(request)

    denied, _ = await server._dispatch(
        "POST", "/v1/routes/retire", {"authorization": "Bearer wrong"}, body
    )
    status, encoded = await server._dispatch(
        "POST",
        "/v1/routes/retire",
        {"authorization": "Bearer separate-route-token"},
        body,
    )

    assert denied == 401
    assert status == 200
    assert decode_scope_retirement_result(request, encoded).result.phase == "completed"
    assert manager.executed == [request]
    assert manager.statuses == []


@pytest.mark.asyncio
async def test_retirement_status_only_reads_and_rejects_malformed_body():
    manager = _ScopeRetirementManager()
    server = PipelineHttpServer(
        None,
        None,
        None,
        scope_retirement_manager=manager,
        routing_control_token="separate-route-token",
    )
    request = _scope_retirement_request()
    status, encoded = await server._dispatch(
        "POST",
        "/v1/routes/retirement-status",
        {"authorization": "Bearer separate-route-token"},
        scope_retirement_request_bytes(request),
    )
    malformed, _ = await server._dispatch(
        "POST",
        "/v1/routes/retirement-status",
        {"authorization": "Bearer separate-route-token"},
        b"{}",
    )

    assert status == 200
    assert decode_scope_retirement_result(request, encoded).state == "unknown"
    assert malformed == 400
    assert manager.statuses == [request]
    assert manager.executed == []
