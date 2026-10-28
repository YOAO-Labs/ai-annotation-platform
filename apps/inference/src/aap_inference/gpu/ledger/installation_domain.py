"""Synchronize a terminal installation's locked Native membership domain."""

from __future__ import annotations

import uuid

from aap_inference.gpu.ledger.store import GPUArbiterStore
from aap_inference.gpu.ledger.types import GPUBackendDomainMember
from aap_inference.gpu.native import GPUAuthorityDataProvider
from aap_inference.gpu.proofs import _gpu_domain_members, _lock_gpu_resource_proof_domain
from aap_inference_contract.gpu import GPUAuthorityUnavailable


async def ensure_installation_domain(
    data: GPUAuthorityDataProvider,
    store: GPUArbiterStore,
    resource_id: str,
    backend_id: str,
    membership_epoch: int,
) -> None:
    """CAS-align Redis with the resource-locked, authoritative Native domain.

    New members enter pending at epoch one before any active promotion. Existing
    Redis members may not disappear or change identity; the target alone may be
    promoted from pending to the same-epoch authoritative active membership. The
    Native resource lock spans both Redis CAS operations.
    """
    async with data.transaction() as transaction:
        locked = await _lock_gpu_resource_proof_domain(transaction, resource_id)
        durable = _gpu_domain_members(locked.memberships)
        authoritative = {member.backend_id: member for member in durable}
        target = authoritative.get(backend_id)
        if (
            target is None
            or target.membership_epoch != membership_epoch
            or target.state != "active"
        ):
            raise GPUAuthorityUnavailable()

        snapshot = await store.snapshot(resource_id)
        current = {member.backend_id: member for member in snapshot.backend_memberships}
        for key, member in current.items():
            expected = authoritative.get(key)
            if expected is None or member.membership_epoch != expected.membership_epoch:
                raise GPUAuthorityUnavailable()
            if member.state != expected.state and not (
                key == backend_id
                and member.state == "pending"
                and expected.state == "active"
            ):
                raise GPUAuthorityUnavailable()

        pending = dict(current)
        for key, member in authoritative.items():
            if key not in pending:
                if member.membership_epoch != 1 or member.state not in {"pending", "active"}:
                    raise GPUAuthorityUnavailable()
                pending[key] = GPUBackendDomainMember(key, 1, "pending")

        async def evolve(members: tuple[GPUBackendDomainMember, ...]) -> None:
            nonlocal snapshot
            result = await store.evolve_backend_domains(
                resource_id,
                expected_ledger_revision=snapshot.ledger_revision,
                expected_ledger_incarnation=snapshot.ledger_incarnation,
                backend_memberships=members,
                evolution_id=uuid.uuid4().hex,
            )
            if result.status not in {"evolved", "unchanged"}:
                raise GPUAuthorityUnavailable()
            snapshot = await store.snapshot(resource_id)
            if snapshot.backend_memberships != members:
                raise GPUAuthorityUnavailable()

        pending_domain = tuple(sorted(pending.values(), key=lambda item: item.backend_id))
        if snapshot.backend_memberships != pending_domain:
            await evolve(pending_domain)
        if snapshot.backend_memberships != durable:
            await evolve(durable)
