from types import SimpleNamespace
from uuid import uuid4

import pytest

from aap_inference.backend_control_manager import BackendControlManager


def _installation(registry_id, device):
    gpu = device == "cuda"
    return {
        "installation_id": str(registry_id), "status": "disabled", "revision": 2,
        "resource": {"gpu_resource_id": "card" if gpu else None,
                     "physical_gpu_uuid": "GPU-example" if gpu else None,
                     "logical_device": "cuda:0" if gpu else "cpu"},
        "manifest": {
            "plugin_id": "detector", "version": "1", "publisher": "example", "license": "MIT",
            "artifact": "example/detector@sha256:" + "a" * 64, "sdk_compatibility": "1",
            "entrypoint": ["detector"], "capabilities": ["inference", "detection"],
            "resource": {"device": device, "memory_mb": 1024, "gpu_memory_mb": 512 if gpu else 0},
            "asset_manifest_sha256": "b" * 64, "allowed_asset_sources": [],
        },
    }


@pytest.mark.asyncio
async def test_cuda_installation_cannot_use_cpu_terminal_route():
    registry_id = uuid4()
    installation = _installation(registry_id, "cuda")

    class Opener:
        async def open(self, *args, **kwargs):
            return SimpleNamespace(
                gpu_resource_id="card",
                transport_configuration=lambda: SimpleNamespace(extra_params={"plugin": installation}),
            )

    manager = object.__new__(BackendControlManager)
    manager.endpoint_openers = {"lifecycle": Opener()}
    request = SimpleNamespace(
        registry_id=registry_id, endpoint_subject=SimpleNamespace(purpose="lifecycle", authority=None),
        request_id=uuid4(), operation="terminal_drain",
    )
    reply = await manager._execute_registered(request)
    assert reply.error.code == "backend_endpoint_unavailable"


@pytest.mark.asyncio
async def test_terminal_route_preserves_fixed_receipt_body():
    calls = []

    class Transport:
        async def terminal_drain(self, body):
            calls.append(body)
            return {**body, "control_epoch": None, "generation": None,
                    "pool_ids": ["models"], "unloaded": True}

    registry_id, operation_id = uuid4(), uuid4()
    body = {"registry_id": str(registry_id), "boot_id": "boot", "operation_id": str(operation_id)}
    request = SimpleNamespace(operation="terminal_drain", body=body)
    result = await object.__new__(BackendControlManager)._registered_operation(request, Transport())
    assert calls == [body]
    assert result == {**body, "control_epoch": None, "generation": None,
                      "pool_ids": ["models"], "unloaded": True}
