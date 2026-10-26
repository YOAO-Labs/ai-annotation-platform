"""Authoritative fail-closed safety checks for destructive backend operations."""

from __future__ import annotations

import uuid

from aap_inference.topology import RegistryTopologySnapshot
from aap_inference_contract.routing import RoutingUnavailable
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.inference_topology_data import InferenceTopologyData
from .authority import RoutingAuthority
from .contracts import MLBackendQuiescenceError, quiescence_error


async def require_registry_quiescent(
    db: AsyncSession, registry_id: uuid.UUID
) -> RegistryTopologySnapshot:
    """Read durable work and verified backend continuity under the owner fence."""
    if await InferenceTopologyData(db).registry_reference(registry_id) is None:
        return RegistryTopologySnapshot(registry_id, None, None, None, None)
    try:
        result = await RoutingAuthority().quiescence(registry_id)
        if result.status == "rejected":
            raise quiescence_error(result.reason)
        if result.status != "ok" or result.topology is None:
            raise RoutingUnavailable()
        topology = result.topology
        return RegistryTopologySnapshot(
            registry_id,
            topology.member_id,
            topology.pool_id,
            topology.routing_generation,
            topology.traffic_state,
        )
    except MLBackendQuiescenceError:
        raise
    except Exception:
        raise MLBackendQuiescenceError(
            "ml_backend_router_unavailable",
            "durable routing owner unavailable; quiescence cannot be proven",
            unavailable=True,
        ) from None
