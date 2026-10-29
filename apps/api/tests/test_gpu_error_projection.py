from __future__ import annotations

import asyncio
import socket
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from aap_inference.gpu.client import GPUAuthorityHTTPClient
from aap_inference.gpu.contracts import GPUArbiterDispatchError, GPUArbiterErrorCode
from aap_inference.pipeline_server import PipelineHttpServer
from aap_inference_contract.gpu import GPUInstallationControlRequest
from app.api.v1.admin_plugin_installations import _gpu_error
from app.services.inference_gpu import _GPUHttpTransport


@pytest.mark.asyncio
async def test_gpu_owner_tcp_error_projects_to_framed_public_http(unused_tcp_port):
    class CapacityLimitedManager:
        async def quiesce_installation(self, request):
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.CAPACITY_UNAVAILABLE,
                message="GPU capacity remains occupied",
                retry_after_s=7,
            )

    owner = PipelineHttpServer(
        None,
        None,
        "control-token",
        gpu_manager=CapacityLimitedManager(),
        gpu_control_token="gpu-token",
        host="127.0.0.1",
        port=unused_tcp_port,
    )
    owner_listener = await owner.start()
    owner_port = owner_listener.sockets[0].getsockname()[1]
    authority = GPUAuthorityHTTPClient(
        f"http://127.0.0.1:{owner_port}", _GPUHttpTransport("gpu-token")
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
    app = FastAPI()

    @app.post("/disable")
    async def disable():
        try:
            await authority.quiesce_installation(request)
        except GPUArbiterDispatchError as error:
            raise _gpu_error(error) from None

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
            lifespan="off",
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    task.result()
                    raise AssertionError("public server stopped before startup")
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(f"http://127.0.0.1:{port}/disable")
        assert response.status_code == 503
        assert response.headers["retry-after"] == "7"
        assert int(response.headers["content-length"]) == len(response.content)
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {
            "detail": {
                "error_code": "gpu_capacity_unavailable",
                "message": "GPU capacity remains occupied",
            }
        }
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        finally:
            listener.close()
            await owner.close()
