"""Borrow the caller Session for the Inference health coordinator's exact phases."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.gpu_backend_membership import GPUBackendMembership
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry


class InferenceHealthData:
    def __init__(
        self, db: AsyncSession, *, get_backend: Callable[[UUID], Awaitable[Any]]
    ) -> None:
        self.db = db
        self._get_backend = get_backend

    async def get_backend(self, registry_id):
        return await self._get_backend(registry_id)

    async def read_membership(self, registry_id, gpu_resource_id, *, for_update=False):
        statement = select(
            GPUBackendMembership.membership_epoch, GPUBackendMembership.state
        ).where(
            GPUBackendMembership.backend_registry_id == registry_id,
            GPUBackendMembership.gpu_resource_id == gpu_resource_id,
            GPUBackendMembership.state.in_(("pending", "active")),
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self.db.execute(statement)).one_or_none()

    async def clock_timestamp(self):
        return await self.db.scalar(select(func.clock_timestamp()))

    async def lock_current_backend(self, registry_id):
        return await self.db.scalar(
            select(MLBackendRegistry)
            .where(MLBackendRegistry.id == registry_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def lock_gpu_resource(self, resource_id):
        await self.db.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('aap:gpu-resource:' || :resource_id, 0))"
            ),
            {"resource_id": resource_id},
        )

    async def try_membership_promotion(self):
        return await self.db.scalar(
            text(
                "SELECT pg_try_advisory_xact_lock("
                "hashtextextended('aap:gpu-membership-promotion', 0))"
            )
        )

    async def write_health(self, registry_id, values):
        result = await self.db.execute(
            update(MLBackendRegistry)
            .where(MLBackendRegistry.id == registry_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount

    async def refresh_backend(self, backend):
        await self.db.refresh(backend)

    async def load_pool_member(self, registry_id):
        return await self.db.scalar(
            select(MLBackendPoolMember)
            .where(MLBackendPoolMember.registry_id == registry_id)
            .with_for_update()
        )

    async def load_pool(self, pool_id):
        return await self.db.scalar(
            select(MLBackendServicePool)
            .where(MLBackendServicePool.id == pool_id)
            .with_for_update()
        )

    async def flush(self):
        await self.db.flush()
