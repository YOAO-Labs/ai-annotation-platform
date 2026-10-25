"""API route adapter over the durable Inference execution owner.

The owner rechecks committed admission, reserves scopes, and settles actual HTTP
attempts. Redis is used only for optional circuit/observe diagnostics. Lease-shaped
values preserve existing caller interfaces while carrying opaque owner scopes.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.gpu_backend_membership import GPUBackendMembership
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
from aap_inference.pool_capability import capability_fingerprint
from app.services.ml_routing.contracts import (
    RejectionReason,
    RouteLease,
    RouteOutcome,
    RouteSelection,
    RouterMode,
    RoutingCandidate,
    RoutingError,
    TrafficState,
)
from app.services.ml_routing.ledger import RoutingLedger

logger = logging.getLogger(__name__)


def _router_mode() -> RouterMode:
    try:
        return RouterMode(settings.ml_backend_router_mode)
    except ValueError:
        logger.warning(
            "invalid ML_BACKEND_ROUTER_MODE=%r; falling back to off",
            settings.ml_backend_router_mode,
        )
        return RouterMode.OFF


class MLBackendRouter:
    """Acquire / finish / cancel route leases for ML Backend service pools.

    One instance per request/job. Holds a DB session + a routing ledger. The ledger
    may be None in off mode (no acquire needed).
    """

    def __init__(
        self,
        db: AsyncSession,
        ledger: RoutingLedger | None = None,
        *,
        mode: RouterMode | None = None,
        authority=None,
    ) -> None:
        self.db = db
        self.ledger = ledger
        self.mode = mode or _router_mode()
        from .authority import RoutingAuthority

        self.authority = authority or RoutingAuthority(db.bind)

    # ── acquire ───────────────────────────────────────────────────────────────
    async def acquire(
        self,
        pool_id: uuid.UUID | None,
        *,
        owner: str,
        operation: str,
        project_id: uuid.UUID | None = None,
        required_instance_id: uuid.UUID | None = None,
        registry_id: uuid.UUID | None = None,
        execution: dict | None = None,
    ) -> RouteSelection:
        """Reserve durable execution ownership using current committed topology."""
        from aap_inference_contract.routing import RoutingUnavailable
        from .authority import RoutingRejected

        try:
            scope = await self.authority.acquire(
                pool_id=pool_id,
                registry_id=registry_id,
                project_id=project_id,
                mode=self.mode.value if pool_id is not None else "direct",
                owner=owner,
                operation=operation,
                required_instance_id=required_instance_id,
                **({"execution": execution} if execution is not None else {}),
            )
        except RoutingRejected as exc:
            if execution is not None and exc.reason == "management_busy":
                return RouteSelection(
                    lease=None,
                    instance_id=None,
                    rejection=RejectionReason.ROUTER_UNAVAILABLE,
                )
            reason = {
                "pool_not_enabled": RejectionReason.POOL_NOT_ENABLED,
                "pool_saturated": RejectionReason.POOL_SATURATED,
                "generation_mismatch": RejectionReason.GENERATION_MISMATCH,
            }.get(exc.reason, RejectionReason.POOL_UNAVAILABLE)
            return RouteSelection(lease=None, instance_id=None, rejection=reason)
        except RoutingUnavailable:
            return RouteSelection(
                lease=None,
                instance_id=None,
                rejection=RejectionReason.ROUTER_UNAVAILABLE,
            )
        lease = RouteLease(
            lease_id=str(scope.id),
            pool_id=scope.pool_id,
            instance_id=scope.registry_id,
            owner=scope.owner,
            operation=scope.operation,
            generation=scope.generation or 0,
            expires_at_ms=scope.heartbeat_deadline_ms,
            scope=scope,
            authority=self.authority,
        )
        selection = RouteSelection(
            lease=lease, instance_id=scope.registry_id, rejection=None
        )
        if (
            self.mode == RouterMode.OBSERVE
            and self.ledger is not None
            and pool_id is not None
        ):
            with self.db.no_autoflush:
                pool = await self.db.get(MLBackendServicePool, pool_id)
                if pool is not None:
                    selection = await self._shadow_would_select(
                        pool, selection, required_instance_id=required_instance_id
                    )
        return selection

    async def _build_candidates(
        self,
        pool: MLBackendServicePool,
        *,
        required_instance_id: uuid.UUID | None = None,
    ) -> list[RoutingCandidate]:
        """Read DB topology under pool.routing_generation → validated candidates."""
        members = await self.db.execute(
            select(MLBackendPoolMember).where(
                MLBackendPoolMember.pool_id == pool.id,
                MLBackendPoolMember.traffic_state == TrafficState.ACTIVE.value,
            )
        )
        member_rows = list(members.scalars())
        singleton_without_fingerprint = (
            pool.capability_fingerprint is None and len(member_rows) == 1
        )
        candidates: list[RoutingCandidate] = []
        now = datetime.now(UTC)
        max_age = timedelta(seconds=settings.ml_backend_router_health_max_age_seconds)
        for member in member_rows:
            if (
                required_instance_id is not None
                and member.registry_id != required_instance_id
            ):
                continue
            registry = await self.db.get(MLBackendRegistry, member.registry_id)
            if registry is None:
                continue
            if registry.state != "connected":
                continue
            # health freshness
            health_fresh = (
                registry.last_checked_at is not None
                and registry.last_checked_at >= now - max_age
            )
            # capability fingerprint exact match (D3)
            caps = (
                (registry.health_meta or {}).get("capabilities")
                if registry.health_meta
                else None
            )
            fingerprint_ok = False
            if pool.capability_fingerprint and caps is not None:
                fingerprint_ok = (
                    capability_fingerprint(caps) == pool.capability_fingerprint
                )
            elif singleton_without_fingerprint and caps is not None:
                # 0132-created singleton pools predate capability seeding. A single
                # member is interchangeable with itself; its next health refresh
                # persists the canonical fingerprint. Multi-member pools stay closed.
                fingerprint_ok = True
            max_conc = await self._max_concurrency_for(registry.id)
            candidates.append(
                RoutingCandidate(
                    instance_id=registry.id,
                    weight=member.weight,
                    max_concurrency=max_conc,
                    fingerprint_ok=fingerprint_ok,
                    health_fresh=health_fresh,
                    traffic_state=TrafficState(member.traffic_state),
                )
            )
        return candidates

    async def _max_concurrency_for(self, registry_id: uuid.UUID) -> int:
        """Source the per-instance concurrency cap. Reads GPUBackendMembership.max_concurrency
        if a membership exists; otherwise falls back to extra_params.max_concurrency or 4."""
        membership = await self.db.execute(
            select(GPUBackendMembership.max_concurrency).where(
                GPUBackendMembership.backend_registry_id == registry_id,
                GPUBackendMembership.state.in_(["pending", "active"]),
            )
        )
        row = membership.first()
        if row and row[0]:
            return int(row[0])
        # fallback for CPU / no-GPU-claim instances
        registry = await self.db.get(MLBackendRegistry, registry_id)
        if registry is not None:
            extra = registry.extra_params or {}
            mc = extra.get("max_concurrency")
            if isinstance(mc, int) and mc > 0:
                return mc
        return 4

    async def _shadow_would_select(
        self,
        pool: MLBackendServicePool,
        legacy_selection: RouteSelection,
        *,
        required_instance_id: uuid.UUID | None = None,
    ) -> RouteSelection:
        """observe-mode: compute what enforce WOULD select, without gating actual dispatch.

        Records diagnostics (actual=legacy vs would-select) for the observe window
        validation gate (ADR-0050 §14). Best-effort: ledger errors are swallowed.
        """
        try:
            candidates = await self._build_candidates(
                pool,
                **(
                    {"required_instance_id": required_instance_id}
                    if required_instance_id is not None
                    else {}
                ),
            )
            if not candidates:
                return legacy_selection
            circuit_open = await self.ledger.circuit_open_instances(str(pool.id))  # type: ignore[union-attr]
            eligible = [
                c
                for c in candidates
                if str(c.instance_id) not in circuit_open
                and c.health_fresh
                and c.fingerprint_ok
            ]
            if not eligible:
                return legacy_selection
            # cheap SWRR preview: pick highest-weight, tie-break smallest UUID.
            winner = min(
                eligible,
                key=lambda c: (-c.weight, str(c.instance_id)),
            )
            return RouteSelection(
                lease=legacy_selection.lease,
                instance_id=legacy_selection.instance_id,
                rejection=None,
                would_select=winner.instance_id,
                diagnostics={
                    "actual_instance": str(legacy_selection.instance_id)
                    if legacy_selection.instance_id
                    else None,
                    "would_select": str(winner.instance_id),
                    "eligible_count": len(eligible),
                },
            )
        except Exception as exc:  # noqa: BLE001 — observe must never break dispatch
            logger.debug("observe would-select skipped: %s", exc)
            return legacy_selection

    async def finish(
        self, lease: RouteLease, outcome: RouteOutcome, duration_ms: int
    ) -> bool:
        if lease is None or lease.scope is None:
            return False
        await lease.authority.close(
            lease.scope, "success" if outcome == RouteOutcome.SUCCESS else "error"
        )
        return True

    async def cancel(self, lease: RouteLease) -> bool:
        if lease is None or lease.scope is None:
            return False
        await lease.authority.close(lease.scope, "cancel")
        return True

    async def heartbeat(self, lease: RouteLease) -> bool:
        if lease is None or lease.scope is None:
            return False
        await lease.authority.execute(
            "heartbeat",
            {"scope_id": lease.lease_id, "capability": lease.scope.capability},
        )
        return True

    # ── legacy resolver (off/observe: registry id → its singleton pool) ───────
    async def pool_for_registry(
        self, registry_id: uuid.UUID
    ) -> MLBackendServicePool | None:
        """Resolve the singleton pool owning a registry (for legacy-id dispatch paths).

        Used by call sites that still carry a registry id (pre-router migration) to find
        the pool so they can record the requested_pool_id on results (ADR-0050 §5.4).
        """
        result = await self.db.execute(
            select(MLBackendServicePool)
            .join(
                MLBackendPoolMember,
                MLBackendPoolMember.pool_id == MLBackendServicePool.id,
            )
            .where(MLBackendPoolMember.registry_id == registry_id)
        )
        return result.scalars().first()


@asynccontextmanager
async def route_context(
    router: MLBackendRouter,
    pool_id: uuid.UUID,
    *,
    owner: str,
    operation: str,
    project_id: uuid.UUID | None = None,
    required_instance_id: uuid.UUID | None = None,
) -> AsyncIterator[tuple[uuid.UUID | None, RouteLease | None, RouteSelection]]:
    """Context manager that acquires a route and guarantees finish/cancel exactly once.

    Yields (instance_id, lease, selection). On normal exit → finish(SUCCESS) if a lease
    exists; on exception → cancel. Caller may override the outcome by calling
    router.finish/cancel themselves before exit (the context detects the idempotent no-op).
    """
    selection = await router.acquire(
        pool_id,
        owner=owner,
        operation=operation,
        project_id=project_id,
        **(
            {"required_instance_id": required_instance_id}
            if required_instance_id is not None
            else {}
        ),
    )
    if selection.rejection is not None:
        raise RoutingError(
            selection.rejection,
            f"route acquire rejected: {selection.rejection.value}",
        )
    lease = selection.lease
    instance_id = selection.instance_id
    from .authority import scope_binding

    try:
        async with scope_binding(router.authority, lease.scope):
            yield instance_id, lease, selection
    except BaseException:
        if lease is not None:
            try:
                await router.cancel(lease)
            except Exception:
                logger.warning("route scope close unacknowledged")
        raise
    else:
        if lease is not None:
            try:
                await router.finish(lease, RouteOutcome.SUCCESS, duration_ms=0)
            except Exception:
                logger.warning("route scope close unacknowledged")


def make_ledger_from_settings() -> RoutingLedger:
    """Build a RoutingLedger from app settings (for API / worker entry points)."""
    return RoutingLedger.from_url(
        settings.redis_url,
        lease_ttl_ms=settings.ml_backend_router_lease_ttl_seconds * 1000,
        heartbeat_interval_ms=settings.ml_backend_router_heartbeat_interval_seconds
        * 1000,
        passive_failure_threshold=settings.ml_backend_router_passive_failure_threshold,
        eject_ms=settings.ml_backend_router_eject_seconds * 1000,
    )
