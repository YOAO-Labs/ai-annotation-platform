from __future__ import annotations

import json as json_module
from uuid import uuid4

import pytest

from aap_inference.gpu.client import GPUAuthorityHTTPClient
from aap_inference.gpu.contracts import GPUArbiterDispatchError
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
            "POST", path, {"authorization": f"Bearer {self.token}"},
            json_module.dumps(json).encode(),
        )
        return _Response(status, body)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["activate", "quiesce"])
async def test_installation_client_and_authenticated_route(operation):
    request = GPUInstallationControlRequest(
        registry_id=uuid4(), resource_id="gpu-1", endpoint="http://backend",
        boot_id="boot-1", operation_id=uuid4(), container_id="a" * 64,
        pool_ids=["pool-1", "pool-2"],
    )
    fields = dict(registry_id=request.registry_id, resource_id=request.resource_id,
                  endpoint=request.endpoint, boot_id=request.boot_id,
                  control_epoch="1", pool_ids=request.pool_ids)
    if operation == "activate":
        reply = GPUInstallationActivationReply(**fields)
        path = GPU_ADMIN_INSTALLATION_ACTIVATE_PATH
    else:
        reply = GPUInstallationQuiesceReply(
            **fields, generation="1", operation_id=request.operation_id,
            container_id=request.container_id, unloaded=True,
        )
        path = GPU_ADMIN_INSTALLATION_QUIESCE_PATH
    manager = _Manager(reply)
    server = PipelineHttpServer(None, None, "control-token", gpu_manager=manager,
                                gpu_control_token="gpu-token")
    transport = _Transport(server)
    client = GPUAuthorityHTTPClient("http://authority", transport)

    result = await getattr(client, f"{operation}_installation")(request)

    assert result == reply
    assert manager.calls == [(operation, request)]
    assert transport.calls == [(path, request.model_dump(mode="json"))]

    status, _ = await server._dispatch("POST", path, {"authorization": "Bearer wrong"},
                                       request.model_dump_json().encode())
    assert status == 401
    status, _ = await server._dispatch("POST", path, {"authorization": "Bearer gpu-token"}, b"{}")
    assert status == 400
    assert manager.calls == [(operation, request)]


@pytest.mark.asyncio
async def test_installation_client_rejects_malformed_reply():
    request = GPUInstallationControlRequest(
        registry_id=uuid4(), resource_id="gpu-1", endpoint="http://backend",
        boot_id="boot-1", operation_id=uuid4(), container_id="a" * 64, pool_ids=["pool-1"],
    )

    class BadTransport:
        async def post(self, url, *, json):
            return _Response(200, b'{}')

    with pytest.raises(GPUArbiterDispatchError):
        await GPUAuthorityHTTPClient("http://authority", BadTransport()).activate_installation(request)
