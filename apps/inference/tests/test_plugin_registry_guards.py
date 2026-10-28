from types import SimpleNamespace
from uuid import uuid4

import pytest

from aap_inference.topology import BackendTopologyCoordinator, ManagedMutationBlocked


class RegistryData:
    def __init__(self, extra):
        self.extra = extra
        self.patches = []

    async def registry_facts(self, registry_id):
        return SimpleNamespace(id=registry_id, extra_params=self.extra)

    async def patch_registry(self, registry_id, patch):
        self.patches.append(patch)
        return SimpleNamespace(id=registry_id, extra_params=patch.get("extra_params", self.extra))

    async def flush(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", [{"plugin": {}}, {"plugin": None}])
async def test_generic_create_rejects_reserved_metadata(reserved):
    coordinator = BackendTopologyCoordinator(RegistryData(None))
    with pytest.raises(ManagedMutationBlocked):
        await coordinator.create_registry("backend", "http://backend", extra_params=reserved)


@pytest.mark.asyncio
@pytest.mark.parametrize("original,patch", [
    ({}, {"extra_params": {"plugin": {"status": "active"}}}),
    ({"plugin": {"status": "active"}}, {"extra_params": {}}),
    ({"plugin": {"status": "active"}}, {"url": "http://other"}),
    ({"plugin": None}, {"name": "other"}),
])
async def test_generic_update_cannot_add_remove_or_mutate_installation(original, patch):
    data = RegistryData(original)
    with pytest.raises(ManagedMutationBlocked):
        await BackendTopologyCoordinator(data).update_registry(uuid4(), **patch)
    assert data.patches == []

@pytest.mark.asyncio
async def test_generic_delete_preserves_historical_plugin_registry():
    coordinator = BackendTopologyCoordinator(RegistryData({"plugin": None}))
    with pytest.raises(ManagedMutationBlocked):
        await coordinator.validate_delete(uuid4())
    with pytest.raises(ManagedMutationBlocked):
        await coordinator.delete_registry(uuid4())



@pytest.mark.asyncio
async def test_legacy_registry_update_is_unaffected():
    data = RegistryData({"max_concurrency": 4})
    result = await BackendTopologyCoordinator(data).update_registry(uuid4(), name="other")
    assert result is not None
    assert data.patches == [{"name": "other"}]
