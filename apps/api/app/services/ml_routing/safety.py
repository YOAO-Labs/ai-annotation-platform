"""Authoritative fail-closed safety checks for destructive backend operations."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from aap_inference.topology import RegistryTopologySnapshot
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.services.ml_routing.router import make_ledger_from_settings


class MLBackendQuiescenceError(Exception):
    def __init__(
        self, error_code: str, message: str, *, unavailable: bool = False
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.unavailable = unavailable

    def as_detail(self) -> dict[str, str]:
        return {"error_code": self.error_code, "message": str(self)}


async def require_registry_quiescent(
    db: AsyncSession, registry_id: uuid.UUID
) -> RegistryTopologySnapshot:
    """Observe enforce + draining + current exact inflight=0 before mutation.

    Unpooled registries cannot receive service-pool leases and retain the legacy
    direct-unload behavior. Known observation failures block the action; Redis continuity is not proved here.
    """
    member = await db.scalar(
        select(MLBackendPoolMember)
        .where(MLBackendPoolMember.registry_id == registry_id)
        .execution_options(populate_existing=True)
    )
    if member is None:
        return RegistryTopologySnapshot(registry_id, None, None, None, None)
    pool = await db.scalar(
        select(MLBackendServicePool)
        .where(MLBackendServicePool.id == member.pool_id)
        .execution_options(populate_existing=True)
    )
    if pool is None:
        raise MLBackendQuiescenceError(
            "ml_backend_pool_unavailable",
            "service-pool membership has no owning pool",
        )
    if settings.ml_backend_router_mode != "enforce":
        raise MLBackendQuiescenceError(
            "ml_backend_member_draining",
            "router must be in enforce mode before a managed backend can be unloaded",
        )
    if member.traffic_state != "draining":
        raise MLBackendQuiescenceError(
            "ml_backend_member_draining",
            "service-pool member must be draining before unload",
        )

    observed = RegistryTopologySnapshot(
        registry_id, member.id, pool.id, pool.routing_generation, member.traffic_state
    )
    ledger = None
    try:
        ledger = make_ledger_from_settings()
        await ledger.healthcheck()
        inflight = await ledger.member_inflight(str(pool.id), str(registry_id))
    except Exception as exc:
        raise MLBackendQuiescenceError(
            "ml_backend_router_unavailable",
            "router ledger is unavailable; quiescence cannot be proven",
            unavailable=True,
        ) from exc
    finally:
        if ledger is not None:
            try:
                await ledger.aclose()
            except Exception:
                # Closing a client cannot invalidate an inflight proof that was
                # already read successfully, and must not mask the primary error.
                pass
    if inflight > 0:
        raise MLBackendQuiescenceError(
            "ml_backend_member_not_quiescent",
            f"service-pool member still has {inflight} active route lease(s)",
        )
    return observed
