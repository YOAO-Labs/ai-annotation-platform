from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ml_backend import MLBackendService
from app.services.ml_routing.safety import require_registry_quiescent
from app.services.workspace_backend_bindings import (
    clear_primary_backend_binding_for_pool,
)


async def delete_registry(db: AsyncSession, registry_id: uuid.UUID) -> bool:
    service = MLBackendService(db)
    if not await service.validate_delete(registry_id):
        return False

    topology = await require_registry_quiescent(db, registry_id)
    result = await service.delete_with_topology(registry_id, expected_topology=topology)
    if result.deleted and result.pool_id is not None:
        await clear_primary_backend_binding_for_pool(db, result.pool_id)
    return result.deleted
