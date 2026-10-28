from types import SimpleNamespace
from uuid import uuid4

import pytest

from aap_inference.backend_control_manager import BackendControlManager


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reload", "warmup", "install_trained_model", "managed_evaluation_seal"])
async def test_inactive_plugin_rejects_work_creating_control_before_transport(operation):
    installation = {
        "installation_id": str(uuid4()), "status": "disabled", "revision": 2,
        "resource": {"gpu_resource_id": None, "physical_gpu_uuid": None, "logical_device": "cpu"},
        "manifest": {
            "plugin_id": "detector", "version": "1", "publisher": "example", "license": "MIT",
            "artifact": "example/detector@sha256:" + "a" * 64, "sdk_compatibility": "1",
            "entrypoint": ["detector"], "capabilities": ["inference", "detection"],
            "resource": {"device": "cpu", "memory_mb": 1024, "gpu_memory_mb": 0},
            "asset_manifest_sha256": "b" * 64, "allowed_asset_sources": [],
        },
    }

    class Opener:
        async def open(self, *args, **kwargs):
            return SimpleNamespace(extra_params={"plugin": installation})

    manager = object.__new__(BackendControlManager)
    manager.endpoint_openers = {"execution": Opener()}
    request = SimpleNamespace(
        endpoint_subject=SimpleNamespace(purpose="execution", authority=None),
        request_id=uuid4(), operation=operation,
    )
    reply = await manager._execute_registered(request)
    assert reply.error.code == "backend_endpoint_unavailable"
    assert reply.error.status_code == 503
