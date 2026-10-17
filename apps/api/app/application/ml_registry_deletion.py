from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ml_backend import MLBackendService
from app.services.ml_routing.safety import require_registry_quiescent
from app.services.project_workspace import clear_primary_backend_binding_for_pool


async def delete_registry(db: AsyncSession, registry_id: uuid.UUID) -> bool:
    service = MLBackendService(db)
    if not await service.validate_delete(registry_id):
        return False

    await require_registry_quiescent(db, registry_id)
    pool_id = await service.pool_id_for_registry(registry_id)
    deleted = await service.delete(registry_id)
    if deleted and pool_id is not None:
        await clear_primary_backend_binding_for_pool(db, pool_id)
    return deleted
