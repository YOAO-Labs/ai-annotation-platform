"""Authoritative fail-closed safety checks for destructive backend operations."""

from __future__ import annotations

import uuid

from aap_inference.topology import RegistryTopologySnapshot
from aap_inference_contract.routing import ROUTING_DATA_PROTOCOL, RoutingUnavailable
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.ml_backend_registry import MLBackendRegistry

from .authority import RoutingAuthority
from .contracts import MLBackendQuiescenceError, quiescence_error


async def require_registry_quiescent(
    db: AsyncSession, registry_id: uuid.UUID
) -> RegistryTopologySnapshot:
    """Read durable work and verified backend continuity under the owner fence."""
    if await db.get(MLBackendRegistry, registry_id) is None:
        return RegistryTopologySnapshot(registry_id, None, None, None, None)
    try:
        async with RoutingAuthority(db.bind).data_transaction() as data:
            result = await data.step("quiescence", {"registry_id": str(registry_id)})
        if result.get("schema") != ROUTING_DATA_PROTOCOL:
            raise RoutingUnavailable()
        if result.get("status") == "rejected":
            raise quiescence_error(result.get("reason"))
        if result.get("status") != "ok":
            raise RoutingUnavailable()
        topology = result["topology"]
        return RegistryTopologySnapshot(
            registry_id,
            uuid.UUID(topology["member_id"]) if topology["member_id"] else None,
            uuid.UUID(topology["pool_id"]) if topology["pool_id"] else None,
            topology["routing_generation"],
            topology["traffic_state"],
        )
    except MLBackendQuiescenceError:
        raise
    except Exception:
        raise MLBackendQuiescenceError(
            "ml_backend_router_unavailable",
            "durable routing owner unavailable; quiescence cannot be proven",
            unavailable=True,
        ) from None
