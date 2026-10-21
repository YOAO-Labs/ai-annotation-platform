"""v0.23.3 ADR-0050 §15 · topology + runtime-snapshot diagnostics for v0.23.4.

These read models are the contract v0.23.4's model-market UI consumes. They join
pool/member/registry/GPU/health by stable IDs (never by URL). topology is role-scoped
(Project Admin sees a trimmed view); runtime-snapshot is Super-Admin-only.

v0.23.4 enrichment (plan §1 / Appendix A.7 — read-only field fixes only):
* both builders return typed Pydantic models (``TopologyResponse`` /
  ``RuntimeSnapshotResponse``) so OpenAPI + generated TS types are real, not ``unknown``;
* topology carries derived ``routable_instances`` / ``status`` / ``status_reason_codes``;
* Project Admin projection is enforced server-side (``routing_policy="unknown"``,
  member ``weight`` / ``state`` / ``last_checked_at`` / ``gpu_resource_id`` → ``None``);
* runtime snapshot carries a freshness envelope (``observed_at`` / ``sources`` /
  ``partial`` / ``partial_reason``) per plan §6.3.

No routing-core changes: the router, route lease, selector and project binding
migration remain v0.23.3's. Metrics-driven fields stay ``None`` (plan §4.2 forbids
wiring shared route counters; v0.23.3's Prometheus module is declared but unwired).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
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
    pools_q = await db.execute(
        select(MLBackendServicePool).order_by(MLBackendServicePool.created_at.desc())
    )
    pools = list(pools_q.scalars().all())
    observed_at = datetime.now(UTC)
    max_age = timedelta(seconds=settings.ml_backend_router_health_max_age_seconds)
    pool_entries: list[TopologyPoolEntry] = []
    for pool in pools:
        members_q = await db.execute(
            select(MLBackendPoolMember, MLBackendRegistry)
            .join(
                MLBackendRegistry,
                MLBackendRegistry.id == MLBackendPoolMember.registry_id,
            )
            .where(MLBackendPoolMember.pool_id == pool.id)
            .order_by(MLBackendRegistry.name)
        )
        member_rows = members_q.all()
        member_models: list[TopologyMemberInstance] = []
        # (traffic_state, registry_connected) tuples for status derivation.
        member_states: list[tuple[str, bool, bool, bool]] = []
        singleton_without_fingerprint = (
            pool.capability_fingerprint is None and len(member_rows) == 1
        )
        for member, registry in member_rows:
            connected = registry.state == "connected"
            health_fresh = (
                registry.last_checked_at is not None
                and registry.last_checked_at >= observed_at - max_age
            )
            caps = (registry.health_meta or {}).get("capabilities")
            fingerprint_ok = bool(
                isinstance(caps, dict)
                and (
                    singleton_without_fingerprint
                    or capability_fingerprint(caps) == pool.capability_fingerprint
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
    """Full runtime snapshot (Super Admin only): router mode + per-pool/member
    inflight / circuit / health + a freshness envelope (plan §6.3).

    Best-effort Redis reads: a ledger/Redis failure flips the ``router_ledger``
    source to ``stale`` + ``error`` rather than dropping the whole snapshot.
    GPU / residency sources are honestly marked ``stale`` with a documented
    reason (``not_bundled_in_v0_23_3``): v0.23.3 does not surface them in the
    snapshot, and v0.23.4 surfaces them via ``/observe`` in the Instance Detail
    Sheet instead. Partial-fail must not erase the trustworthy sources.
    """
    mode = settings.ml_backend_router_mode
    observed_at = datetime.now(UTC)
    max_age = timedelta(seconds=settings.ml_backend_router_health_max_age_seconds)

    pools_q = await db.execute(select(MLBackendServicePool))
    pools = list(pools_q.scalars().all())
    pool_snapshots: list[RuntimePoolSnapshot] = []

    ledger_error: str | None = None
    any_health_stale = False
    health_timestamps: list[datetime] = []

    if mode != "off" and ledger is not None:
        try:
            await ledger.healthcheck()
        except Exception as exc:  # noqa: BLE001
            ledger_error = str(exc) or "redis_unavailable"

    for pool in pools:
        members_q = await db.execute(
            select(MLBackendPoolMember, MLBackendRegistry)
            .join(
                MLBackendRegistry,
                MLBackendRegistry.id == MLBackendPoolMember.registry_id,
            )
            .where(MLBackendPoolMember.pool_id == pool.id)
        )
        member_snapshots: list[RuntimeMemberSnapshot] = []
        for member, registry in members_q.all():
            health_fresh = (
                registry.last_checked_at is not None
                and registry.last_checked_at >= observed_at - max_age
            )
            if not health_fresh:
                any_health_stale = True
            if registry.last_checked_at is not None:
                health_timestamps.append(registry.last_checked_at)
            # route inflight from Redis ledger (best-effort; observe mode may
            # have no ledger; ledger/Redis failure is captured for the source).
            inflight: int | None = None
            circuit_open: bool | None = None
            if ledger is not None and ledger_error is None:
                try:
                    inflight = await ledger.member_inflight(
                        str(pool.id), str(registry.id)
                    )
                    circuit = await ledger._redis.hgetall(
                        f"{ledger.namespace}:pool:{pool.id}:member:{registry.id}:circuit"
                    )
                    circuit_open = circuit.get("state") == "open" and int(
                        circuit.get("open_until", 0)
                    ) > int(datetime.now(UTC).timestamp() * 1000)
                except Exception as exc:  # noqa: BLE001 — capture for source flag
                    ledger_error = str(exc) or "redis_unavailable"
                    inflight = None
                    circuit_open = None
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
    # router_ledger: fresh only when mode != off AND ledger present AND no Redis error.
    if mode == "off":
        # router is off → ledger reads are intentionally absent, not stale.
        sources.append(
            SourceFreshness(name="router_ledger", updated_at=observed_at, stale=False)
        )
    elif ledger is None or ledger_error is not None:
        sources.append(
            SourceFreshness(
                name="router_ledger",
                updated_at=None,
                stale=True,
                error=ledger_error or "ledger_unavailable",
            )
        )
    else:
        sources.append(
            SourceFreshness(name="router_ledger", updated_at=observed_at, stale=False)
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
