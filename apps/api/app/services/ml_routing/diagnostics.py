"""Role-scoped topology and durable routing observations for the model market.

The routing owner supplies execution counts in every mode. Redis supplies only
optional circuit hints; neither observation authorizes lifecycle management.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from aap_inference_contract.topology import to_record
from app.config import settings
from app.services.inference_topology_data import InferenceTopologyData
from app.schemas.ml_routing import (
    PoolAvailabilityStatus,
    RuntimeMemberSnapshot,
    RuntimePoolSnapshot,
    RuntimeSnapshotResponse,
    SourceFreshness,
    TopologyMemberInstance,
    TopologyPoolEntry,
    TopologyResponse,
)
from app.services.ml_routing.ledger import RoutingLedger
from aap_inference.pool_capability import capability_fingerprint


def _derive_pool_status(
    member_states: list[tuple[str, bool, bool, bool]],
    *,
    pool_enabled: bool,
) -> tuple[PoolAvailabilityStatus, list[str], int]:
    """Derive ``(status, reason_codes, routable_instances)`` from member tuples.

    ``member_states`` is a list of ``(traffic_state, registry_connected)`` where
    ``registry_connected`` is True when ``registry.state == "connected"``.

    This is a *display hint* assembled from server-side fields; it does not
    invent routing truth. Reasons are stable codes the frontend can group on.

    Rules:
    * no members → ``unknown`` (a pool with zero members has no routable state);
    * every member ``disabled`` → ``offline``;
    * any member ``draining`` or any ``connected`` member not routable → ``degraded``;
    * otherwise → ``healthy``.
    """
    if not member_states:
        return "unknown", ["empty_pool"], 0

    routable = sum(
        1
        for state, connected, health_fresh, fingerprint_ok in member_states
        if pool_enabled
        and state == "active"
        and connected
        and health_fresh
        and fingerprint_ok
    )
    disabled_count = sum(1 for state, *_ in member_states if state == "disabled")
    draining_count = sum(1 for state, *_ in member_states if state == "draining")
    disconnected_active = sum(
        1
        for state, connected, *_ in member_states
        if state == "active" and not connected
    )
    stale_active = sum(
        1 for state, _, fresh, _ in member_states if state == "active" and not fresh
    )
    mismatch_active = sum(
        1 for state, _, _, match in member_states if state == "active" and not match
    )

    reason_codes: list[str] = []
    if disabled_count:
        reason_codes.append(f"{disabled_count}_disabled")
    if draining_count:
        reason_codes.append(f"{draining_count}_draining")
    if disconnected_active:
        reason_codes.append(f"{disconnected_active}_disconnected")
    if stale_active:
        reason_codes.append(f"{stale_active}_health_stale")
    if mismatch_active:
        reason_codes.append(f"{mismatch_active}_capability_mismatch")
    if not pool_enabled:
        reason_codes.append("pool_disabled")

    if routable == 0:
        status: PoolAvailabilityStatus = "offline"
    elif routable < len(member_states) or reason_codes:
        status = "degraded"
    else:
        status = "healthy"
    return status, reason_codes, routable


async def build_topology(db: AsyncSession, *, super_admin: bool) -> TopologyResponse:
    """Pool/member/registry topology, role-scoped.

    Project Admin gets a trimmed view: pool name/member_count/enabled/
    routable_instances/status only; member identity + traffic_state only.
    Super Admin gets routing_policy/capability_fingerprint/legacy_instance_id
    plus member weight/state/last_checked_at/gpu_resource_id.
    """
    topology = InferenceTopologyData(db)
    pools = sorted(
        await topology.pool_catalog(), key=lambda pool: pool.created_at, reverse=True
    )
    observed_at = datetime.now(UTC)
    max_age = timedelta(seconds=settings.ml_backend_router_health_max_age_seconds)
    pool_entries: list[TopologyPoolEntry] = []
    for pool in pools:
        member_rows = sorted(
            await topology.pool_members(pool.id),
            key=lambda member: member.registry.name,
        )
        member_models: list[TopologyMemberInstance] = []
        # (traffic_state, registry_connected) tuples for status derivation.
        member_states: list[tuple[str, bool, bool, bool]] = []
        singleton_without_fingerprint = (
            pool.capability_fingerprint is None and len(member_rows) == 1
        )
        for member in member_rows:
            registry = member.registry
            connected = registry.state == "connected"
            health_fresh = (
                registry.last_checked_at is not None
                and registry.last_checked_at >= observed_at - max_age
            )
            caps = (registry.health_meta or {}).get("capabilities")
            fingerprint_ok = bool(
                isinstance(caps, Mapping)
                and (
                    singleton_without_fingerprint
                    or capability_fingerprint(to_record(caps))
                    == pool.capability_fingerprint
                )
            )
            member_states.append(
                (member.traffic_state, connected, health_fresh, fingerprint_ok)
            )
            member_models.append(
                TopologyMemberInstance(
                    registry_id=registry.id,
                    name=registry.name,
                    traffic_state=member.traffic_state,
                    weight=member.weight if super_admin else None,
                    state=registry.state if super_admin else None,
                    last_checked_at=registry.last_checked_at if super_admin else None,
                    gpu_resource_id=registry.gpu_resource_id if super_admin else None,
                )
            )
        status, reason_codes, routable = _derive_pool_status(
            member_states, pool_enabled=pool.enabled
        )
        pool_entries.append(
            TopologyPoolEntry(
                id=pool.id,
                name=pool.name,
                enabled=pool.enabled,
                routing_policy=pool.routing_policy if super_admin else "unknown",
                legacy_instance_id=pool.legacy_instance_id if super_admin else None,
                routing_generation=pool.routing_generation,
                capability_fingerprint=(
                    pool.capability_fingerprint if super_admin else None
                ),
                member_count=len(member_models),
                routable_instances=routable,
                status=status,
                status_reason_codes=reason_codes if super_admin else [],
                members=member_models,
            )
        )
    return TopologyResponse(
        generated_at=observed_at,
        router_mode=settings.ml_backend_router_mode,
        pools=pool_entries,
    )


async def build_runtime_snapshot(
    db: AsyncSession, ledger: RoutingLedger | None
) -> RuntimeSnapshotResponse:
    """Read durable execution counts; keep Redis circuit hints non-authoritative."""
    mode = settings.ml_backend_router_mode
    observed_at = datetime.now(UTC)
    max_age = timedelta(seconds=settings.ml_backend_router_health_max_age_seconds)

    topology = InferenceTopologyData(db)
    pools = await topology.pool_catalog()
    pool_snapshots: list[RuntimePoolSnapshot] = []

    from .authority import RoutingAuthority

    member_groups = {}
    for pool in pools:
        member_groups[pool.id] = await topology.pool_members(pool.id)
    registry_ids = sorted(
        {member.registry.id for members in member_groups.values() for member in members}
    )
    execution_rows = {}
    routing_error = None
    routing_observed_at = observed_at
    authority = RoutingAuthority()
    try:
        for offset in range(0, len(registry_ids), 512):
            reply = await authority.runtime_snapshot(
                registry_ids[offset : offset + 512]
            )
            routing_observed_at = min(routing_observed_at, reply.observed_at)
            execution_rows.update({row.registry_id: row for row in reply.registries})
        if set(execution_rows) != set(registry_ids):
            routing_error = "routing_registry_unavailable"
    except Exception:
        routing_error = "routing_owner_unavailable"

    any_health_stale = False
    health_timestamps: list[datetime] = []
    circuit_available = ledger is not None
    if circuit_available:
        try:
            await ledger.healthcheck()
        except Exception:
            circuit_available = False

    for pool in pools:
        member_snapshots: list[RuntimeMemberSnapshot] = []
        for member in member_groups[pool.id]:
            registry = member.registry
            health_fresh = (
                registry.last_checked_at is not None
                and registry.last_checked_at >= observed_at - max_age
            )
            if not health_fresh:
                any_health_stale = True
            if registry.last_checked_at is not None:
                health_timestamps.append(registry.last_checked_at)
            execution = execution_rows.get(registry.id)
            inflight = execution.open_scopes if execution is not None else None
            circuit_open: bool | None = None
            if circuit_available:
                try:
                    circuit = await ledger._redis.hgetall(
                        f"{ledger.namespace}:pool:{pool.id}:member:{registry.id}:circuit"
                    )
                    circuit_open = circuit.get("state") == "open" and int(
                        circuit.get("open_until", 0)
                    ) > int(observed_at.timestamp() * 1000)
                except Exception:
                    circuit_available = False
            member_snapshots.append(
                RuntimeMemberSnapshot(
                    registry_id=registry.id,
                    name=registry.name,
                    traffic_state=member.traffic_state,
                    weight=member.weight,
                    registry_state=registry.state,
                    health_fresh=health_fresh,
                    last_checked_at=registry.last_checked_at,
                    route_inflight=inflight,
                    circuit_open=circuit_open,
                    gpu_resource_id=registry.gpu_resource_id,
                    # Metrics-driven fields stay None (plan §4.2 / Appendix A.2).
                )
            )
        pool_snapshots.append(
            RuntimePoolSnapshot(
                id=pool.id,
                name=pool.name,
                enabled=pool.enabled,
                routing_generation=pool.routing_generation,
                members=member_snapshots,
            )
        )

    # ── freshness envelope (plan Appendix A.2) ────────────────────────────
    sources: list[SourceFreshness] = []
    sources.append(
        SourceFreshness(
            name="topology",
            updated_at=observed_at,
            stale=False,
        )
    )
    sources.append(
        SourceFreshness(
            name="router_ledger",
            updated_at=routing_observed_at if routing_error is None else None,
            stale=routing_error is not None,
            error=routing_error,
        )
    )
    # health: stale if any member's last_checked_at exceeds the freshness window.
    sources.append(
        SourceFreshness(
            name="health",
            updated_at=min(health_timestamps) if health_timestamps else None,
            stale=any_health_stale,
            error=None if not any_health_stale else "some_member_health_stale",
        )
    )
    # gpu / residency: v0.23.3 does not bundle them in the snapshot. Honest gap.
    sources.append(
        SourceFreshness(
            name="gpu",
            updated_at=None,
            stale=True,
            error="not_bundled_in_v0_23_3",
        )
    )
    sources.append(
        SourceFreshness(
            name="residency",
            updated_at=None,
            stale=True,
            error="not_bundled_in_v0_23_3",
        )
    )

    partial_flags = [s for s in sources if s.stale]
    partial = bool(partial_flags)
    partial_reason: str | None = None
    if partial:
        names = [s.name for s in partial_flags]
        partial_reason = (
            f"{len(partial_flags)}/{len(sources)} sources stale: {', '.join(names)}"
        )

    return RuntimeSnapshotResponse(
        observed_at=observed_at,
        router_mode=mode,
        partial=partial,
        partial_reason=partial_reason,
        sources=sources,
        pools=pool_snapshots,
    )
