"""Single-command HTTP recovery checks for the routing owner client."""

from __future__ import annotations

import json

import httpx
import pytest
import uuid

from aap_inference_contract.routing import (
    ROUTING_RESULT_PROTOCOL,
    ROUTING_RUNTIME_RESULT_PROTOCOL,
    RoutingResult,
    RoutingUnavailable,
    RoutingVideoExecutionProof,
    decode_routing_command,
    decode_routing_runtime_request,
    decode_routing_status_request,
    make_routing_command,
    make_routing_status_request,
    make_routing_runtime_request,
    routing_command_sha256,
    routing_result_bytes,
    routing_runtime_result_bytes,
)
from aap_inference_contract.routing_client import RoutingHttpClient
from aap_inference_contract.scope_retirement import (
    SCOPE_RETIREMENT_RESULT_PROTOCOL,
    ScopeRetirementResult,
    ScopeRetirementView,
    make_scope_retirement_request,
    scope_retirement_request_bytes,
    scope_retirement_result_bytes,
)
from aap_inference_contract.video_execution import (
    VIDEO_EXECUTION_DATA_PROTOCOL,
    VIDEO_EXECUTION_STATUS_PATH,
    decode_video_execution_status_request,
    video_execution_status_request_bytes,
    video_execution_status_result_bytes,
)

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


def _runtime_result(request):
    return {
        "schema": ROUTING_RUNTIME_RESULT_PROTOCOL,
        "request_id": request.request_id,
        "observed_at": "2026-10-22T02:30:00Z",
        "registries": [
            {
                "registry_id": _REGISTRY_ID,
                "open_scopes": 1,
                "active_attempts": 1,
                "uncertain_attempts": 0,
                "continuity": "ready",
                "authority_epoch": _EPOCH_ID,
                "backend_incarnation": None,
                "control_id": None,
            }
        ],
    }


def _video_proof():
    return RoutingVideoExecutionProof(
        job_id=uuid.UUID("10b9f6cc-6424-4b4a-8740-3660cb25db17"),
        run_id=uuid.UUID("9f8f00b4-6d2f-4fe0-a75e-a28c4e923714"),
        worker_id=uuid.UUID("8f87cb4e-636d-4606-983f-e7397cb954f9"),
        generation=4,
        lease_token=uuid.UUID("cb72f60a-61b0-4e76-b355-f8e8b976c4a2"),
    )


def _video_status_data(proof):
    scope_id = uuid.UUID("153d8835-975e-4a2e-b75f-fb9e9b0fc834")
    registry_id = uuid.UUID("0cab28f1-b3dc-4c94-88e4-0481dc99ca09")
    epoch_id = uuid.UUID("ef85556e-8977-4096-82b0-d229a3e026df")
    return {
        "schema": VIDEO_EXECUTION_DATA_PROTOCOL,
        "status": "ok",
        "intent": {
            "job_id": str(proof.job_id),
            "run_id": str(proof.run_id),
            "generation": proof.generation,
            "request_id": "5dca07e4-533d-4aec-aee6-23bb6ea79454",
            "command_sha256": "a" * 64,
            "scope_id": str(scope_id),
            "state": "bound",
        },
        "scope": {
            "id": str(scope_id),
            "pool_id": None,
            "registry_id": str(registry_id),
            "project_id": None,
            "mode": "direct",
            "owner": f"tracker:{proof.job_id}",
            "operation": "tracker",
            "generation": None,
            "config_revision": 1,
            "eligibility_revision": 1,
            "authority_epoch": str(epoch_id),
            "state": "reserved",
            "closed": False,
            "heartbeat_deadline_ms": 1782090000000,
            "capability": "opaque-scope-capability",
        },
    }


def test_video_execution_status_request_requires_exact_five_proof_fields():
    proof = _video_proof()
    encoded = video_execution_status_request_bytes(proof)
    assert decode_video_execution_status_request(encoded) == proof

    malformed = encoded.replace(
        b'"job_id":',
        b'"job_id":"' + str(proof.job_id).encode() + b'","job_id":',
    )
    with pytest.raises(RoutingUnavailable):
        decode_video_execution_status_request(malformed)


@pytest.mark.asyncio
async def test_video_execution_status_uses_authenticated_bound_read_and_echoes_full_proof(
    monkeypatch,
):
    proof = _video_proof()
    data = _video_status_data(proof)
    requests = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == VIDEO_EXECUTION_STATUS_PATH
        assert request.headers["authorization"] == "Bearer private-routing-token"
        typed_proof = decode_video_execution_status_request(await request.aread())
        assert typed_proof == proof
        return httpx.Response(
            200,
            content=video_execution_status_result_bytes(proof, data),
            headers={"content-type": "application/json"},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    result = await RoutingHttpClient(
        "http://routing-owner.local", "private-routing-token"
    ).video_execution_status(proof)

    assert result["intent"].job_id == proof.job_id
    assert result["scope"].owner == f"tracker:{proof.job_id}"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_video_execution_status_rejects_response_for_different_full_proof(
    monkeypatch,
):
    proof = _video_proof()
    encoded = video_execution_status_result_bytes(proof, _video_status_data(proof))
    reply = json.loads(encoded)
    reply["proof"]["worker_id"] = str(_REGISTRY_ID)

    async def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=reply,
            headers={"content-type": "application/json"},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    with pytest.raises(RoutingUnavailable):
        await RoutingHttpClient(
            "http://routing-owner.local", "token"
        ).video_execution_status(proof)


@pytest.mark.asyncio
async def test_runtime_posts_one_bound_observation_request_without_recovery(
    monkeypatch,
) -> None:
    request = make_routing_runtime_request([_REGISTRY_ID], request_id=_REQUEST_ID)
    calls = []

    async def handle(http_request: httpx.Request) -> httpx.Response:
        calls.append(http_request)
        assert http_request.url.path == "/base/v1/routes/runtime"
        assert http_request.headers["authorization"] == "Bearer private-token"
        assert decode_routing_runtime_request(await http_request.aread()) == request
        return httpx.Response(
            200,
            content=routing_runtime_result_bytes(_runtime_result(request)),
            headers={"content-type": "application/json"},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    client = RoutingHttpClient("http://routing-owner.local/base", "private-token")

    result = await client.runtime(request)

    assert result.request_id == request.request_id
    assert result.registries[0].open_scopes == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_runtime_lost_reply_is_not_retried_or_treated_as_a_command(
    monkeypatch,
) -> None:
    request = make_routing_runtime_request([_REGISTRY_ID], request_id=_REQUEST_ID)
    calls = 0

    async def lose_reply(http_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert http_request.url.path == "/v1/routes/runtime"
        raise httpx.ReadError("runtime reply lost", request=http_request)

    _install_mock_transport(monkeypatch, httpx.MockTransport(lose_reply))
    client = RoutingHttpClient("http://routing-owner.local", "private-token")

    with pytest.raises(RoutingUnavailable):
        await client.runtime(request)

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


def _retirement_request():
    return make_scope_retirement_request(
        "6f1a1afd-80f4-4e7d-a7bc-f2d13e392f56",
        "560f02f7-1378-4708-b3a7-6992047c1fb6",
        "private-scope-capability",
    )


def _retirement_result(request, *, phase="draining"):
    return ScopeRetirementResult(
        schema=SCOPE_RETIREMENT_RESULT_PROTOCOL,
        retirement_id=request.retirement_id,
        scope_id=request.scope_id,
        state="committed",
        result=ScopeRetirementView(
            retirement_id=request.retirement_id,
            scope_id=request.scope_id,
            registry_id=_REGISTRY_ID,
            authority_epoch=_EPOCH_ID,
            backend_incarnation=uuid.UUID("be189eb2-2e7d-4c36-94b0-20ad78d68147"),
            phase=phase,
            closed=True,
        ),
    )


@pytest.mark.asyncio
async def test_retire_scope_uses_selected_bearer_and_bound_result(monkeypatch):
    request = _retirement_request()
    calls = []

    async def handle(http_request: httpx.Request) -> httpx.Response:
        calls.append(http_request)
        assert http_request.url.path == "/base/v1/routes/retire"
        assert http_request.headers["authorization"] == "Bearer private-routing-token"
        assert await http_request.aread() == scope_retirement_request_bytes(request)
        return httpx.Response(
            200,
            content=scope_retirement_result_bytes(_retirement_result(request)),
            headers={"content-type": "application/json"},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    client = RoutingHttpClient(
        "http://routing-owner.local/base", "private-routing-token"
    )

    result = await client.retire_scope(request)

    assert result.result.phase == "draining"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_retire_scope_lost_ack_uses_one_read_and_never_reposts(monkeypatch):
    request = _retirement_request()
    paths = []

    async def handle(http_request: httpx.Request) -> httpx.Response:
        paths.append(http_request.url.path)
        if http_request.url.path.endswith("/retire"):
            raise httpx.ReadError(
                "retirement acknowledgement lost", request=http_request
            )
        assert http_request.url.path.endswith("/retirement-status")
        return httpx.Response(
            200,
            content=scope_retirement_result_bytes(
                _retirement_result(request, phase="completed")
            ),
            headers={"content-type": "application/json"},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    client = RoutingHttpClient("http://routing-owner.local", "private-token")

    result = await client.retire_scope(request)

    assert result.result.phase == "completed"
    assert paths == ["/v1/routes/retire", "/v1/routes/retirement-status"]


@pytest.mark.asyncio
async def test_scope_retirement_status_is_one_read_only_request(monkeypatch):
    request = _retirement_request()
    calls = []

    async def handle(http_request: httpx.Request) -> httpx.Response:
        calls.append(http_request.url.path)
        assert http_request.url.path == "/v1/routes/retirement-status"
        return httpx.Response(
            200,
            content=scope_retirement_result_bytes(
                ScopeRetirementResult(
                    schema=SCOPE_RETIREMENT_RESULT_PROTOCOL,
                    retirement_id=request.retirement_id,
                    scope_id=request.scope_id,
                    state="unknown",
                    result=None,
                )
            ),
            headers={"content-type": "application/json"},
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(handle))
    client = RoutingHttpClient("http://routing-owner.local", "private-token")

    result = await client.scope_retirement_status(request)

    assert result.state == "unknown"
    assert calls == ["/v1/routes/retirement-status"]
