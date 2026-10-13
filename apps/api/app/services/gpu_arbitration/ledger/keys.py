"""Redis key layout and builders for the GPU arbitration ledger.

Owns the ledger's Redis key layout and builders. Depends on
:mod:`gpu_arbitration.ledger.types` (constants, key dataclass inputs) and the
``_validate_nonempty`` helper from :mod:`gpu_arbitration.ledger.validation`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.services.gpu_arbitration.ledger.types import (
    _DEFAULT_NAMESPACE,
    _NAMESPACE_RE,
)
from app.services.gpu_arbitration.ledger.validation import _validate_nonempty


@dataclass(frozen=True)
class GPUArbiterKeys:
    resource_id: str
    resource_tag: str
    card: str
    allocations: str
    queue: str
    transition: str
    namespace: str

    def backend_queue(self, backend_id: str) -> str:
        return f"{self.namespace}:{{{self.resource_tag}}}:backend_queue:{backend_id}"

    def leases(self, backend_id: str) -> str:
        return f"{self.namespace}:{{{self.resource_tag}}}:leases:{backend_id}"

    def tombstone_gc_receipt(
        self,
        backend_id: str,
        membership_epoch: int,
        retirement_id: str,
    ) -> str:
        return (
            f"{self.namespace}:{{{self.resource_tag}}}:tombstone_gc_receipt:"
            f"{backend_id}:{membership_epoch}:{retirement_id}"
        )


def gpu_arbiter_keys(
    resource_id: str, *, namespace: str = _DEFAULT_NAMESPACE
) -> GPUArbiterKeys:
    _validate_nonempty(resource_id, "resource_id")
    if not isinstance(namespace, str) or _NAMESPACE_RE.fullmatch(namespace) is None:
        raise ValueError("invalid GPU arbiter Redis namespace")
    # Raw resource ids may legally contain braces today. A stable digest keeps every
    # key for this exact resource in one brace-safe Cluster hash slot.
    resource_tag = hashlib.sha256(resource_id.encode("utf-8")).hexdigest()
    prefix = f"{namespace}:{{{resource_tag}}}"
    return GPUArbiterKeys(
        resource_id=resource_id,
        resource_tag=resource_tag,
        card=f"{prefix}:card",
        allocations=f"{prefix}:allocations",
        queue=f"{prefix}:queue",
        transition=f"{prefix}:transition",
        namespace=namespace,
    )
