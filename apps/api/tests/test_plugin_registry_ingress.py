from types import MappingProxyType, SimpleNamespace
from uuid import uuid4

import pytest

from app.services.ml_backend import GPUBackendManagedMutationBlocked, MLBackendService


class ExistingRegistry:
    def __init__(self, extra):
        self.row = SimpleNamespace(id=uuid4(), extra_params=extra)

    async def get_registry(self, registry_id):
        return self.row


@pytest.mark.asyncio
async def test_generic_create_rejects_plugin_before_credential_handshake():
    service = MLBackendService(None)
    with pytest.raises(GPUBackendManagedMutationBlocked):
        await service.create_registry(
            "plugin", "http://backend", auth_method="managed:yolo",
            extra_params={"plugin": {"status": "active"}},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("extra,patch", [
    (MappingProxyType({"plugin": MappingProxyType({"status": "active"})}), {"url": "http://other"}),
    ({"plugin": None}, {"extra_params": {}}),
    ({}, {"extra_params": {"plugin": {"status": "active"}}}),
])
async def test_generic_update_rejects_before_endpoint_handshake(monkeypatch, extra, patch):
    service = MLBackendService(None)
    topology = ExistingRegistry(extra)
    monkeypatch.setattr(service, "_topology", lambda: topology)
    with pytest.raises(GPUBackendManagedMutationBlocked):
        await service.update(topology.row.id, **patch)
