"""GPU arbitration Redis ledger package.

Public surface re-exported here is the ledger's stable API for orchestration callers.
The package root stays minimal and does NOT eager-import orchestration-only modules;
callers import the concrete submodule they need
(e.g. ``from app.services.gpu_arbitration.ledger.store import GPUArbiterStore``).
"""

from app.services.gpu_arbitration.ledger.keys import GPUArbiterKeys, gpu_arbiter_keys
from app.services.gpu_arbitration.ledger.store import GPUArbiterStore
from app.services.gpu_arbitration.ledger.types import (
    GPU_COLD_ADMISSION_OPERATION,
    GPU_EVICTION_OPERATION,
    GPUAdmissionResult,
    GPUAllocation,
    GPUAllocationState,
    GPUArbiterStoreError,
    GPUBackendDomainEvolutionResult,
    GPUBackendDomainMember,
    GPUBackendMembershipState,
    GPUCardSnapshot,
    GPUEvictionBranchResult,
    GPUIdleEvictionResult,
    GPULeaseMutationResult,
    GPUProofResetCAS,
    GPUProofResetContext,
    GPUQueueResult,
    GPUQueueTicket,
    GPUReconcileLeaseCleanup,
    GPUReconcileResult,
    GPURequestLease,
    GPURequestLeaseState,
    GPUTombstoneGCReceipt,
    GPUTombstoneGCResult,
    GPUTransitionOwnerResult,
    GPUTransitionResult,
)
from app.services.gpu_arbitration.ledger.validation import (
    normalize_gpu_backend_max_concurrency,
)

__all__ = [
    "GPU_COLD_ADMISSION_OPERATION",
    "GPU_EVICTION_OPERATION",
    "GPUAdmissionResult",
    "GPUAllocation",
    "GPUAllocationState",
    "GPUArbiterKeys",
    "GPUArbiterStore",
    "GPUArbiterStoreError",
    "GPUBackendDomainEvolutionResult",
    "GPUBackendDomainMember",
    "GPUBackendMembershipState",
    "GPUCardSnapshot",
    "GPUEvictionBranchResult",
    "GPUIdleEvictionResult",
    "GPULeaseMutationResult",
    "GPUProofResetCAS",
    "GPUProofResetContext",
    "GPUQueueResult",
    "GPUQueueTicket",
    "GPUReconcileLeaseCleanup",
    "GPUReconcileResult",
    "GPURequestLease",
    "GPURequestLeaseState",
    "GPUTombstoneGCReceipt",
    "GPUTombstoneGCResult",
    "GPUTransitionOwnerResult",
    "GPUTransitionResult",
    "gpu_arbiter_keys",
    "normalize_gpu_backend_max_concurrency",
]
