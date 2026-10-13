"""Domain types, enums, dataclasses and constants for the GPU arbitration Redis ledger.

This is the primitive layer: it depends on no other ledger submodule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, TypeVar

_DEFAULT_NAMESPACE = "gpu-arbiter:v1"
_NAMESPACE_RE = re.compile(r"[A-Za-z0-9:._-]{1,160}\Z")
_CANONICAL_POSITIVE_INT64_RE = re.compile(r"[1-9][0-9]{0,18}\Z")
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_POSITIVE_INT64 = 9_223_372_036_854_775_807
_MAX_REDIS_SAFE_INTEGER = 9_007_199_254_740_991
_LEDGER_REVISION_REBASE_THRESHOLD = _MAX_REDIS_SAFE_INTEGER - 2_000_000
_MAX_TTL_MS = 2_147_483_647
_TOMBSTONE_GC_RECEIPT_TTL_MS = 7 * 24 * 60 * 60 * 1000
_MAX_GPU_BACKENDS_PER_RESOURCE = 64
_MAX_GPU_BACKEND_CONCURRENCY = 10_000
_MAX_GPU_QUEUE_LENGTH = 10_000
_REDIS_OPERATION_TIMEOUT_SECONDS = 1.0
_REDIS_CALL_DEADLINE_SECONDS = 2.0
_SNAPSHOT_MAX_ATTEMPTS = 32
GPU_COLD_ADMISSION_OPERATION = "cold_admit"
GPU_EVICTION_OPERATION = "evict"
_RedisResultT = TypeVar("_RedisResultT")
_COUNTED_ALLOCATION_STATES = frozenset(
    {
        "unknown",
        "reserving",
        "loading",
        "resident",
        "draining",
        "unloading",
    }
)


class GPUAllocationState(str, Enum):
    UNKNOWN = "unknown"
    UNLOADED = "unloaded"
    RESERVING = "reserving"
    LOADING = "loading"
    RESIDENT = "resident"
    DRAINING = "draining"
    UNLOADING = "unloading"
    CPU_FALLBACK = "cpu_fallback"


class GPURequestLeaseState(str, Enum):
    ACTIVE = "active"
    UNCERTAIN = "uncertain"
    STALE = "stale"


GPUBackendMembershipState = Literal["pending", "active", "retiring"]
_GPU_BACKEND_MEMBERSHIP_STATES = frozenset({"pending", "active", "retiring"})


class GPUArbiterStoreError(RuntimeError):
    """The Redis ledger is unavailable, corrupt, or returned an invalid response."""


@dataclass(frozen=True)
class GPUBackendDomainMember:
    backend_id: str
    membership_epoch: int
    state: GPUBackendMembershipState


@dataclass(frozen=True)
class GPUAllocation:
    backend_id: str
    state: GPUAllocationState
    budget_mb: int
    generation: str | None
    eviction_priority: int
    evictable: bool
    max_concurrency: int
    reservation_lease_id: str | None
    reservation_owner_id: str | None
    last_used_at_ms: int
    not_evict_before_ms: int

    @property
    def counted(self) -> bool:
        return self.state.value in _COUNTED_ALLOCATION_STATES


@dataclass(frozen=True)
class GPURequestLease:
    lease_id: str
    backend_id: str
    owner_id: str
    generation: str
    operation: str
    state: GPURequestLeaseState
    created_at_ms: int
    heartbeat_deadline_ms: int
    hard_deadline_ms: int


@dataclass(frozen=True)
class GPUQueueTicket:
    ticket_id: str
    backend_id: str
    owner_id: str
    kind: Literal["backend", "card"]
    membership_epoch: int
    enqueued_at_ms: int
    expires_at_ms: int


@dataclass(frozen=True)
class GPUAdmissionResult:
    status: Literal[
        "admitted",
        "not_ready",
        "capacity_unavailable",
        "concurrency_saturated",
        "concurrency_queued",
        "card_queued",
        "transition_in_progress",
        "stale_generation",
        "lease_conflict",
        "config_mismatch",
        "ledger_corrupt",
    ]
    reason: str
    committed_mb: int
    lease_count: int
    allocation_state: GPUAllocationState | None = None
    heartbeat_deadline_ms: int | None = None
    hard_deadline_ms: int | None = None
    idempotent: bool = False

    @property
    def admitted(self) -> bool:
        return self.status == "admitted"


@dataclass(frozen=True)
class GPUQueueResult:
    status: Literal[
        "queued",
        "cancelled",
        "missing",
        "full",
        "owner_mismatch",
        "ticket_conflict",
        "not_ready",
        "config_mismatch",
        "ledger_corrupt",
    ]
    ticket_id: str
    position: int | None = None
    expires_at_ms: int | None = None


@dataclass(frozen=True)
class GPUTransitionResult:
    status: Literal[
        "transitioned",
        "missing",
        "stale_generation",
        "invalid_transition",
        "branch_conflict",
        "active_leases",
        "owner_mismatch",
        "not_ready",
        "ledger_corrupt",
    ]
    state: GPUAllocationState | None
    generation: str | None
    committed_mb: int
    idempotent: bool = False


@dataclass(frozen=True)
class GPUTransitionOwnerResult:
    status: Literal[
        "acquired",
        "renewed",
        "released",
        "busy",
        "missing",
        "owner_mismatch",
        "stale_generation",
        "operation_mismatch",
        "active_leases",
        "invalid_transition",
        "ledger_corrupt",
        "not_ready",
        "config_mismatch",
    ]
    owner_id: str | None = None
    generation: str | None = None
    expires_at_ms: int | None = None
    idempotent: bool = False


@dataclass(frozen=True)
class GPUIdleEvictionResult:
    status: Literal[
        "selected",
        "capacity_available",
        "capacity_unavailable",
        "cooldown_active",
        "card_queued",
        "victim_busy",
        "stale_selection",
        "transition_in_progress",
        "not_ready",
        "config_mismatch",
        "ledger_corrupt",
    ]
    reason: str
    committed_mb: int
    shortfall_mb: int
    victim_backend_id: str | None = None
    victim_generation: str | None = None
    victim_budget_mb: int | None = None
    owner_id: str | None = None
    owner_expires_at_ms: int | None = None
    owner_hard_deadline_ms: int | None = None
    retry_at_ms: int | None = None
    idempotent: bool = False


@dataclass(frozen=True)
class GPUEvictionBranchResult:
    status: Literal[
        "armed",
        "missing",
        "stale_generation",
        "owner_mismatch",
        "branch_conflict",
        "invalid_transition",
        "not_ready",
        "ledger_corrupt",
    ]
    branch: Literal["cancel", "unload"] | None = None
    state: GPUAllocationState | None = None
    generation: str | None = None
    idempotent: bool = False


@dataclass(frozen=True)
class GPUReconcileLeaseCleanup:
    observed_idle_at_ms: int
    lease_ids: tuple[str, ...]


@dataclass(frozen=True)
class GPUReconcileResult:
    status: Literal[
        "prepared",
        "reconciled",
        "not_ready",
        "stale_revision",
        "partial_state",
        "busy",
        "active_leases",
        "config_mismatch",
        "stale_generation",
        "ledger_corrupt",
    ]
    ready: bool
    ledger_revision: int
    ledger_incarnation: str
    committed_mb: int
    purged_leases: int = 0
    reason: str = ""
    idempotent: bool = False


@dataclass(frozen=True)
class GPUProofResetContext:
    resource_id: str
    allocatable_mb: int
    reset_id: str
    begin_fingerprint: str
    ledger_revision: int
    ledger_incarnation: str
    prepared_at_ms: int
    backend_ids: tuple[str, ...]
    active_backend_ids: tuple[str, ...]
    backend_memberships: tuple[GPUBackendDomainMember, ...]


@dataclass(frozen=True)
class GPUProofResetCAS:
    ledger_revision: int
    ledger_incarnation: str


@dataclass(frozen=True)
class GPUBackendDomainEvolutionResult:
    status: Literal[
        "evolved",
        "unchanged",
        "not_ready",
        "stale_revision",
        "partial_state",
        "config_mismatch",
        "ledger_corrupt",
    ]
    ledger_revision: int
    ledger_incarnation: str
    requested_backend_ids: tuple[str, ...]
    requested_active_backend_ids: tuple[str, ...]
    requested_backend_memberships: tuple[GPUBackendDomainMember, ...]
    reason: str = ""
    idempotent: bool = False


@dataclass(frozen=True)
class GPUTombstoneGCResult:
    status: Literal[
        "collected",
        "blocked",
        "not_ready",
        "stale_revision",
        "config_mismatch",
        "ledger_corrupt",
    ]
    ledger_revision: int
    ledger_incarnation: str
    backend_id: str
    membership_epoch: int
    reason: str = ""
    idempotent: bool = False


@dataclass(frozen=True)
class GPUTombstoneGCReceipt:
    ledger_revision: int
    ledger_incarnation: str
    backend_id: str
    membership_epoch: int
    retirement_id: str
    fingerprint: str


@dataclass(frozen=True)
class GPULeaseMutationResult:
    status: Literal[
        "heartbeated",
        "uncertain",
        "released",
        "missing",
        "owner_mismatch",
        "stale_generation",
        "stale",
        "not_ready",
        "reservation_active",
    ]
    lease_state: GPURequestLeaseState | None = None
    heartbeat_deadline_ms: int | None = None
    hard_deadline_ms: int | None = None


@dataclass(frozen=True)
class GPUCardSnapshot:
    resource_id: str
    observed_at_ms: int
    allocatable_mb: int
    ready: bool
    reconcile_deadline_ms: int
    ledger_revision: int
    ledger_incarnation: str
    committed_mb: int
    backend_ids: tuple[str, ...]
    active_backend_ids: tuple[str, ...]
    backend_memberships: tuple[GPUBackendDomainMember, ...]
    allocations: tuple[GPUAllocation, ...]
    leases: tuple[GPURequestLease, ...]
    not_ready_reason: str | None
    card_queue_count: int
    backend_queue_count: int
    transition_present: bool
    card_queue: tuple[GPUQueueTicket, ...] = ()
    backend_queues: tuple[GPUQueueTicket, ...] = ()
    transition: dict[str, Any] | None = None


@dataclass(frozen=True)
class _GPUBackendDomains:
    backend_domain_raw: str
    backend_domain_fingerprint: str
    membership_domain_raw: str
    membership_domain_fingerprint: str
    active_backend_domain_raw: str
    active_backend_domain_fingerprint: str
    backend_ids: tuple[str, ...]
    active_backend_ids: tuple[str, ...]
    backend_memberships: tuple[GPUBackendDomainMember, ...]
    ledger_incarnation: str
