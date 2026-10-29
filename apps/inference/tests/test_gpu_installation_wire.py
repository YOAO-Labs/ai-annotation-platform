from __future__ import annotations

import json as json_module
from uuid import uuid4

import httpx
import pytest

from aap_inference.gpu.client import GPUAuthorityHTTPClient
from aap_inference.gpu.contracts import GPUArbiterDispatchError, GPUArbiterErrorCode
from aap_inference.pipeline_server import PipelineHttpServer
from aap_inference_contract.gpu import (
    GPU_ADMIN_INSTALLATION_ACTIVATE_PATH,
    GPU_ADMIN_INSTALLATION_QUIESCE_PATH,
    GPUInstallationActivationReply,
    GPUInstallationControlRequest,
    GPUInstallationQuiesceReply,
)


class _Manager:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def activate_installation(self, request):
        self.calls.append(("activate", request))
        return self.reply

    async def quiesce_installation(self, request):
        self.calls.append(("quiesce", request))
        return self.reply


class _Response:
    def __init__(self, status_code, content):
        self.status_code = status_code
        self.content = content
        self.headers = {}


class _Transport:
    def __init__(self, server, token="gpu-token"):
        self.server = server
        self.token = token
        self.calls = []

    async def post(self, url, *, json):
        path = url.removeprefix("http://authority")
        self.calls.append((path, json))
        status, body = await self.server._dispatch(
            "POST",
            path,
            {"authorization": f"Bearer {self.token}"},
            json_module.dumps(json).encode(),
        )
        return _Response(status, body)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["activate", "quiesce"])
async def test_installation_routes_reject_invalid_authority_before_execution(operation):
    request = GPUInstallationControlRequest(
        registry_id=uuid4(),
        resource_id="gpu-1",
        endpoint="http://backend",
        boot_id="boot-1",
        operation_id=uuid4(),
        container_id="a" * 64,
        pool_ids=["pool-1", "pool-2"],
    )
    fields = dict(
        registry_id=request.registry_id,
        resource_id=request.resource_id,
        endpoint=request.endpoint,
        boot_id=request.boot_id,
        control_epoch="1",
        pool_ids=request.pool_ids,
    )
    if operation == "activate":
        reply = GPUInstallationActivationReply(**fields)
        path = GPU_ADMIN_INSTALLATION_ACTIVATE_PATH
    else:
        reply = GPUInstallationQuiesceReply(
            **fields,
            generation="1",
            operation_id=request.operation_id,
            container_id=request.container_id,
            unloaded=True,
        )
        path = GPU_ADMIN_INSTALLATION_QUIESCE_PATH
    manager = _Manager(reply)
    server = PipelineHttpServer(
        None, None, "control-token", gpu_manager=manager, gpu_control_token="gpu-token"
    )
    transport = _Transport(server)
    client = GPUAuthorityHTTPClient("http://authority", transport)

    await getattr(client, f"{operation}_installation")(request)

    status, _ = await server._dispatch(
        "POST",
        path,
        {"authorization": "Bearer wrong"},
        request.model_dump_json().encode(),
    )
    assert status == 401
    status, _ = await server._dispatch(
        "POST", path, {"authorization": "Bearer gpu-token"}, b"{}"
    )
    assert status == 400
    assert len(manager.calls) == 1


@pytest.mark.asyncio
async def test_installation_client_rejects_malformed_reply():
    request = GPUInstallationControlRequest(
        registry_id=uuid4(),
        resource_id="gpu-1",
        endpoint="http://backend",
        boot_id="boot-1",
        operation_id=uuid4(),
        container_id="a" * 64,
        pool_ids=["pool-1"],
    )

    class BadTransport:
        async def post(self, url, *, json):
            return _Response(200, b"{}")

    with pytest.raises(GPUArbiterDispatchError):
        await GPUAuthorityHTTPClient(
            "http://authority", BadTransport()
        ).activate_installation(request)


@pytest.mark.asyncio
async def test_gpu_capacity_rejection_preserves_retry_contract_over_http(
    unused_tcp_port,
):
    class CapacityLimitedManager:
        async def quiesce_installation(self, request):
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.CAPACITY_UNAVAILABLE,
                message="GPU capacity remains occupied",
                retry_after_s=7,
            )

    request = GPUInstallationControlRequest(
        registry_id=uuid4(),
        resource_id="gpu-1",
        endpoint="http://backend",
        boot_id="boot-1",
        operation_id=uuid4(),
        container_id="a" * 64,
        pool_ids=["pool-1"],
    )
    server = PipelineHttpServer(
        None,
        None,
        "control-token",
        gpu_manager=CapacityLimitedManager(),
        gpu_control_token="gpu-token",
        host="127.0.0.1",
        port=unused_tcp_port,
    )
    listener = await server.start()
    port = listener.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                f"http://127.0.0.1:{port}{GPU_ADMIN_INSTALLATION_QUIESCE_PATH}",
                headers={"Authorization": "Bearer gpu-token"},
                json=request.model_dump(mode="json"),
            )
        assert response.status_code == 503
        failure = response.json()
        assert failure["error_code"] == "gpu_capacity_unavailable"
        assert failure["detail"]["message"] == "GPU capacity remains occupied"
        assert failure["headers"] == {"Retry-After": "7"}
        assert failure["retry_after_s"] == 7
    finally:
        await server.close()


def test_gpu_client_projects_typed_error_headers_case_insensitively():
    response = _Response(
        503,
        json_module.dumps(
            {
                "status_code": 503,
                "detail": {
                    "error_code": "gpu_capacity_unavailable",
                    "nested": {"a": 1},
                },
                "headers": {
                    "Retry-After": "7",
                    "X-Custom": "body",
                    "CoNtEnT-LeNgTh": "2",
                    "cONnection": "X-Body-Hop",
                    "x-body-hop": "private",
                },
                "error_code": "gpu_capacity_unavailable",
                "retry_after_s": 7,
            }
        ).encode(),
    )
    response.headers = {
        "retry-after": "3",
        "x-custom": "transport",
        "X-Other": "keep",
        "CONTENT-TYPE": "application/json",
        "Content-Encoding": "identity",
        "content-length": "123",
        "Transfer-Encoding": "chunked",
        "Connection": "close, X-Private",
        "x-PRIVATE": "secret",
        "Keep-Alive": "timeout=5",
        "Trailer": "x-end",
        "Upgrade": "h2c",
        "Proxy-Authenticate": "private",
    }
    with pytest.raises(GPUArbiterDispatchError) as caught:
        GPUAuthorityHTTPClient._raise_remote_error(response)
    error = caught.value
    assert error.status_code == 503
    assert error.error_code == "gpu_capacity_unavailable"
    assert error.retry_after_s == 7
    assert error.detail == {
        "error_code": "gpu_capacity_unavailable",
        "nested": {"a": 1},
    }
    assert {key.lower(): value for key, value in error.headers.items()} == {
        "retry-after": "7",
        "x-custom": "body",
        "x-other": "keep",
    }


@pytest.mark.parametrize(
    "body,status",
    [
        (b"not json", 503),
        (b'{"status_code":503,"detail":{},"error_code":""}', 503),
        (
            b'{"status_code":503,"detail":{},"error_code":"gpu_capacity_unavailable","extra":1}',
            503,
        ),
        (
            b'{"status_code":409,"detail":{},"error_code":"gpu_capacity_unavailable"}',
            503,
        ),
    ],
)
def test_gpu_client_rejects_malformed_or_mismatched_errors(body, status):
    response = _Response(status, body)
    response.headers = {"Retry-After": "99"}
    with pytest.raises(GPUArbiterDispatchError) as caught:
        GPUAuthorityHTTPClient._raise_remote_error(response)
    assert caught.value.error_code == GPUArbiterErrorCode.UNAVAILABLE.value
    assert caught.value.status_code == 503
    assert caught.value.headers == {}
