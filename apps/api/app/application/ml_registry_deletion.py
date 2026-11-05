from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from yoao_inference.topology import RegistryTopologySnapshot

from app.services.ml_backend import MLBackendService
from app.services.ml_routing.safety import require_registry_quiescent
from app.services.workspace_backend_bindings import (
    clear_primary_backend_binding_for_pool,
)


async def _prepare_registry_deletion(
    db: AsyncSession, registry_id: uuid.UUID
) -> RegistryTopologySnapshot | None:
    if not await MLBackendService(db).validate_delete(registry_id):
        return None
    return await require_registry_quiescent(db, registry_id)


async def _finish_registry_deletion(
    db: AsyncSession, topology: RegistryTopologySnapshot
) -> bool:
    result = await MLBackendService(db).delete_with_topology(
        topology.registry_id, expected_topology=topology
    )
    if result.deleted and result.pool_id is not None:
        await clear_primary_backend_binding_for_pool(db, result.pool_id)
    return result.deleted


async def delete_registry(db: AsyncSession, registry_id: uuid.UUID) -> bool:
    topology = await _prepare_registry_deletion(db, registry_id)
    if topology is None:
        return False
    return await _finish_registry_deletion(db, topology)
