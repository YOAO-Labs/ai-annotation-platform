"""ADR-0050 §3 · typed read models for the model-market UI.

These Pydantic schemas replace the untyped ``dict`` returns of
``build_topology`` / ``build_runtime_snapshot`` so that:

* the OpenAPI snapshot exposes real component schemas (not ``object``),
* the generated TypeScript types are named and typed (not ``unknown``),
* role projection (Project Admin vs Super Admin) is enforced by the schema
  field defaults rather than by key omission,
* a freshness envelope (``observed_at`` / ``stale`` / ``partial`` /
  ``sources``) is part of the contract per plan §3.2/§6.3.

Conventions mirror ``app.schemas.ml_backend``: PEP 604 unions, ``Literal``
for closed enumerations, ``Field(default_factory=...)`` for collections,
and ``None`` defaults for absent/projected-away fields.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# ── status enums (plan Appendix A.1 — four independent axes) ──────────────

TrafficState = Literal["active", "draining", "disabled"]
RoutingMode = Literal["off", "observe", "enforce"]

# Derived pool-level availability, combining health + routing axes. This is a
# *display hint* assembled from already-server-trimmed fields; it never
# invents routing truth the router did not produce.
PoolAvailabilityStatus = Literal["healthy", "degraded", "offline", "unknown"]

# Per-source freshness label (plan Appendix A.2).
SourceName = Literal["topology", "router_ledger", "health", "gpu", "residency"]


# ── topology (role-scoped; consumed by registry management) ───────────────


class TopologyMemberInstance(BaseModel):
    """One pool member (a backend instance).

    Project Admin sees only the identity + traffic fields; Super Admin
    additionally sees ``state`` / ``last_checked_at`` / ``gpu_resource_id``
    (server-side projection sets them to ``None`` for Project Admin, so the
    generated TS type is uniform and the field is simply absent of value).
    """

    registry_id: UUID
    name: str
    traffic_state: TrafficState
    # Super-Admin-only; ``None`` when projected away for Project Admin
    # (plan §5 + Appendix A.6 hide weight/state/health/GPU from Project Admin).
    weight: int | None = None
    state: str | None = None
    last_checked_at: datetime | None = None
    gpu_resource_id: str | None = None


class TopologyPoolEntry(BaseModel):
    """One service pool with derived availability fields.

    ``routable_instances`` / ``status`` / ``status_reason_codes`` are derived
    server-side from the member list (plan Appendix A.6 requirement: the API
    must return the derived reason, the frontend only displays).
    """

    id: UUID
    name: str
    enabled: bool
    # Project Admin projection: ``"unknown"`` (plan Appendix A.6).
    routing_policy: str
    legacy_instance_id: UUID | None = None
    routing_generation: int
    capability_fingerprint: str | None = None
    member_count: int
    # Derived fields:
    routable_instances: int
    status: PoolAvailabilityStatus
    status_reason_codes: list[str] = Field(default_factory=list)
    members: list[TopologyMemberInstance] = Field(default_factory=list)


class TopologyResponse(BaseModel):
    """Role-scoped topology read model (``GET /admin/ml-integrations/topology``)."""

    schema_version: Literal["topology.v1"] = "topology.v1"
    generated_at: datetime
    router_mode: RoutingMode
    pools: list[TopologyPoolEntry] = Field(default_factory=list)


# ── runtime snapshot (Super Admin only; consumed by runtime observe) ──────


class RuntimeMemberSnapshot(BaseModel):
    """One pool member's runtime state.

    Metrics-driven fields (``last_selected_at`` / ``selection_count_window``
    / ``rejection_count_window`` / ``p95_ms`` / ``error_rate``) are ``None``
    when routing metrics are unavailable. The model market displays
    "暂无路由指标" for these values rather than reporting zero activity.
    """

    registry_id: UUID
    name: str
    traffic_state: TrafficState
    weight: int = 1
    registry_state: str
    health_fresh: bool
    last_checked_at: datetime | None = None
    # None means the router ledger could not provide a fresh proof.  It must not
    # be coerced to zero/closed because unload safety is fail-closed.
    route_inflight: int | None = None
    circuit_open: bool | None = None
    gpu_resource_id: str | None = None
    # These fields are unset when routing metrics are unavailable.
    last_selected_at: datetime | None = None
    selection_count_window: int | None = None
    rejection_count_window: int | None = None
    p95_ms: float | None = None
    error_rate: float | None = None


class RuntimePoolSnapshot(BaseModel):
    """One pool's runtime snapshot (members carry runtime state)."""

    id: UUID
    name: str
    enabled: bool
    routing_generation: int
    members: list[RuntimeMemberSnapshot] = Field(default_factory=list)


class SourceFreshness(BaseModel):
    """Per-source freshness entry (plan §6.3 — partial failure must not erase
    other trustworthy sources)."""

    name: SourceName
    updated_at: datetime | None = None
    stale: bool = False
    error: str | None = None


class RuntimeSnapshotResponse(BaseModel):
    """Full runtime snapshot read model
    (``GET /admin/ml-integrations/runtime-snapshot``)."""

    schema_version: Literal["runtime_snapshot.v1"] = "runtime_snapshot.v1"
    observed_at: datetime
    router_mode: RoutingMode
    partial: bool = False
    partial_reason: str | None = None
    sources: list[SourceFreshness] = Field(default_factory=list)
    pools: list[RuntimePoolSnapshot] = Field(default_factory=list)
