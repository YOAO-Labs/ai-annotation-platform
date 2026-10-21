"""v0.23.3 ADR-0050 §B.3 · routing contracts: enums, dataclasses, error codes.

Frozen in P0 (plan appendix §B). P2/P3 implement against these types; deviation needs
an ADR amendment. These are pure data types — no Redis / DB / network access.

Key invariants (ADR-0050 D1-D18):
- pool id is the logical request identity; registry id is the physical execution identity.
- route lease is independent of GPU request lease (D7).
- only ``traffic_state=active`` members may receive new leases (D5).
- caller cancel ≠ transport failure: cancel never trips the passive circuit (D14).
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from typing import Any


# ── Rollout mode (deployment-level single switch, ADR-0050 D17) ────────────────
class RouterMode(str, enum.Enum):
    """``ML_BACKEND_ROUTER_MODE``: off / observe / enforce.

    off / observe keep legacy instance dispatch (behavior = v0.23.2); observe additionally
    computes would-select in a shadow namespace and records diagnostics without gating.
    enforce uses router-selected instances and fails closed on Redis/topology uncertainty.
    """

    OFF = "off"
    OBSERVE = "observe"
    ENFORCE = "enforce"


# ── Member traffic state (ADR-0050 D5 / §10.3) ─────────────────────────────────
class TrafficState(str, enum.Enum):
    ACTIVE = "active"  # may receive new route leases
    DRAINING = "draining"  # keeps existing leases, no new ones
    DISABLED = "disabled"  # no new leases; resume needs re-validation


# ── Routing outcome (fed to finish(); drives circuit + metrics) ────────────────
class RouteOutcome(str, enum.Enum):
    """Terminal outcome of a route lease.

    Only transport-failure outcomes trip the passive circuit (ADR-0050 D14):
    business 4xx, model validation failure, GPU-capacity 503, and explicit cancel do NOT.
    """

    SUCCESS = "success"
    # transport failures → passive circuit
    CONNECT_REFUSED = "connect_refused"
    TRANSPORT_TIMEOUT = "transport_timeout"
    NO_RESPONSE = "no_response"
    GATEWAY_UNAVAILABLE = "gateway_unavailable"
    # non-circuit outcomes
    BUSINESS_ERROR = "business_error"  # 4xx from backend
    MODEL_VALIDATION_ERROR = "model_validation_error"
    GPU_CAPACITY_UNAVAILABLE = "gpu_capacity_unavailable"  # ADR-0049 503
    CANCEL = "cancel"  # caller-initiated cancel; never a transport failure

    @property
    def is_transport_failure(self) -> bool:
        return self in {
            RouteOutcome.CONNECT_REFUSED,
            RouteOutcome.TRANSPORT_TIMEOUT,
            RouteOutcome.NO_RESPONSE,
            RouteOutcome.GATEWAY_UNAVAILABLE,
        }


TRANSPORT_FAILURE_OUTCOMES = frozenset(
    {
        RouteOutcome.CONNECT_REFUSED,
        RouteOutcome.TRANSPORT_TIMEOUT,
        RouteOutcome.NO_RESPONSE,
        RouteOutcome.GATEWAY_UNAVAILABLE,
    }
)


# ── Rejection reasons (structured; returned when acquire selects nothing) ──────
class RejectionReason(str, enum.Enum):
    """Why an acquire returned no instance. Maps to ADR-0050 §B.3 error codes."""

    POOL_NOT_ENABLED = "ml_backend_pool_not_enabled"
    POOL_UNAVAILABLE = "ml_backend_pool_unavailable"  # no active/healthy member
    POOL_SATURATED = "ml_backend_pool_saturated"  # all at max concurrency
    ROUTER_UNAVAILABLE = "ml_backend_router_unavailable"  # Redis down in enforce
    GENERATION_MISMATCH = "generation_mismatch"  # topology changed mid-acquire
    ALL_CIRCUITS_OPEN = "all_circuits_open"


# ── HTTP status for each rejection (ADR-0050 §B.3) ─────────────────────────────
REJECTION_HTTP_STATUS: dict[RejectionReason, int] = {
    RejectionReason.POOL_NOT_ENABLED: 403,
    RejectionReason.POOL_UNAVAILABLE: 503,
    RejectionReason.POOL_SATURATED: 503,
    RejectionReason.ROUTER_UNAVAILABLE: 503,
    RejectionReason.GENERATION_MISMATCH: 503,
    RejectionReason.ALL_CIRCUITS_OPEN: 503,
}


# ── Data classes ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RoutingCandidate:
    """One eligible member at acquire time, validated by the caller (DB-side).

    The router builds candidates from DB topology under a single ``routing_generation``
    then passes them to the Redis acquire Lua which atomically excludes circuit-open
    and concurrency-saturated ones. All fields are caller-asserted before acquire.
    """

    instance_id: uuid.UUID
    weight: int  # 1..100
    max_concurrency: int  # instance-level cap (from GPUBackendMembership or config)
    fingerprint_ok: bool  # capability fingerprint exact-matches pool snapshot
    health_fresh: bool  # health_meta within ML_BACKEND_ROUTER_HEALTH_MAX_AGE_SECONDS
    traffic_state: TrafficState


@dataclass(frozen=True)
class RouteLease:
    """A granted route lease. Caller must finish() or cancel() exactly once."""

    lease_id: str
    pool_id: uuid.UUID | None
    instance_id: uuid.UUID
    owner: str  # caller identity (e.g. "api:request_id" / "celery:task_id")
    operation: str  # e.g. "predict" / "interactive" / "batch" / "tracker"
    generation: int
    expires_at_ms: int
    scope: Any = field(default=None, repr=False, compare=False)
    authority: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class RouteSelection:
    """Result of MLBackendRouter.acquire(): either a lease+instance or a rejection."""

    lease: RouteLease | None
    instance_id: uuid.UUID | None  # selected registry instance (None on rejection)
    rejection: RejectionReason | None
    would_select: uuid.UUID | None = None  # observe-mode shadow selection (instance id)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def acquired(self) -> bool:
        return self.lease is not None


@dataclass(frozen=True)
class CapabilityDiff:
    """Structured diff between a pool's canonical snapshot and a candidate's fingerprint.

    Returned with 409 ``ml_backend_pool_capability_mismatch`` (ADR-0050 §7.3).
    """

    pool_fingerprint: str
    candidate_fingerprint: str
    differing_fields: tuple[str, ...]  # canonical field names that differ


class CapabilityMismatchError(Exception):
    """Raised when adding a member whose fingerprint diverges from the pool snapshot.

    Carries the structured diff for the 409 response body (ADR-0050 §7.3 / §B.3).
    """

    def __init__(self, diff: CapabilityDiff) -> None:
        self.diff = diff
        super().__init__(
            f"capability mismatch: {len(diff.differing_fields)} field(s) differ "
            f"({', '.join(diff.differing_fields[:5])})"
        )

    def as_detail(self) -> dict:
        return {
            "error_code": "ml_backend_pool_capability_mismatch",
            "message": str(self),
            "pool_fingerprint": self.diff.pool_fingerprint,
            "candidate_fingerprint": self.diff.candidate_fingerprint,
            "differing_fields": list(self.diff.differing_fields),
        }


class RoutingError(Exception):
    """Base for routing-domain errors surfaced to callers (API → HTTP error code)."""

    def __init__(
        self, reason: RejectionReason, message: str, *, retry_after: int | None = None
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.http_status = REJECTION_HTTP_STATUS[reason]
        self.retry_after = retry_after

    def as_detail(self) -> dict:
        detail: dict = {"error_code": self.reason.value, "message": str(self)}
        if self.retry_after is not None:
            detail["retry_after"] = self.retry_after
        return detail


class MLBackendQuiescenceError(Exception):
    def __init__(
        self, error_code: str, message: str, *, unavailable: bool = False
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.unavailable = unavailable

    def as_detail(self) -> dict[str, str]:
        return {"error_code": self.error_code, "message": str(self)}


def quiescence_error(reason):
    return MLBackendQuiescenceError(
        "ml_backend_member_draining"
        if reason == "member_not_draining"
        else "ml_backend_" + reason,
        {
            "member_not_draining": "service-pool member must be draining before unload",
            "member_not_quiescent": "backend still has unresolved execution scopes",
            "continuity_unverified": "authenticated backend drain and bind is required before management",
            "management_busy": "backend management is already in progress",
            "generation_mismatch": "service-pool topology changed; drain again and retry",
        }.get(reason, "backend management precondition rejected"),
    )
