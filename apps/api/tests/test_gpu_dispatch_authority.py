from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import time
import uuid

import pytest
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    AdmissionTokenClaims,
    DrainTransitionResponse,
    ManagedUnloadResponse,
)

from app.config import Settings
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.gpu_arbitration import dispatch as authority_module
from app.services.gpu_arbitration.contracts import (
    GPUArbiterDispatchError,
    GPUArbiterErrorCode,
    GPUDispatchRequest,
)
from app.services.gpu_arbitration.proofs import (
    GPUColdRuntimeSubject,
    GPUColdTerminalCommitResult,
    GPUEvictionCommitResult,
    GPUEvictionDrainHealth,
    GPUIdleEvictionRuntimeSubject,
    GPUPreparedColdRuntimeSubject,
    GPUPreparedEvictionCancelRuntimeSubject,
    GPUPreparedIdleEvictionRuntimeSubject,
    GPUResidentRuntimeSubject,
    GPUResidentRuntimeSubjectError,
)
from app.services.gpu_arbitration.ledger import (
    GPUAdmissionResult,
    GPUAllocation,
    GPUAllocationState,
    GPUArbiterStoreError,
    GPUCardSnapshot,
    GPUEvictionBranchResult,
    GPUIdleEvictionResult,
    GPULeaseMutationResult,
    GPUQueueResult,
    GPURequestLease,
    GPURequestLeaseState,
    GPUTransitionOwnerResult,
    GPUTransitionResult,
)


_RESOURCE_ID = "node-runtime/index:0"


def _subject() -> GPUResidentRuntimeSubject:
    return GPUResidentRuntimeSubject(
        backend_registry_id=uuid.uuid4(),
        gpu_resource_id=_RESOURCE_ID,
        membership_epoch=3,
        budget_mb=4096,
        eviction_priority=2,
        max_concurrency=4,
        boot_id="boot-runtime",
        generation="7",
        control_epoch="5",
        runtime_epoch="2",
        db_now=datetime.now(UTC),
    )


def _cold_subject() -> GPUColdRuntimeSubject:
    return GPUColdRuntimeSubject(
        backend_registry_id=uuid.uuid4(),
        gpu_resource_id=_RESOURCE_ID,
        membership_epoch=3,
        budget_mb=4096,
        eviction_priority=2,
        max_concurrency=4,
        boot_id="boot-runtime",
        observed_generation="7",
        generation_high_water=7,
        control_epoch="5",
        runtime_epoch="2",
        db_now=datetime.now(UTC),
    )


def _prepared_cold_subject(
    subject: GPUColdRuntimeSubject,
) -> GPUPreparedColdRuntimeSubject:
    return GPUPreparedColdRuntimeSubject(
        backend_registry_id=subject.backend_registry_id,
        gpu_resource_id=subject.gpu_resource_id,
        membership_epoch=subject.membership_epoch,
        budget_mb=subject.budget_mb,
        eviction_priority=subject.eviction_priority,
        max_concurrency=subject.max_concurrency,
        boot_id=subject.boot_id,
        observed_generation=subject.observed_generation,
        generation=str(subject.generation_high_water + 1),
        control_epoch=subject.control_epoch,
        runtime_epoch=subject.runtime_epoch,
        token_expires_at=subject.db_now + timedelta(seconds=120),
        db_now=subject.db_now,
    )


def _allocation(
    backend_id: uuid.UUID,
    *,
    budget_mb: int,
    generation: str,
    eviction_priority: int = 1,
    last_used_at_ms: int = 1,
    not_evict_before_ms: int = 1,
) -> GPUAllocation:
    return GPUAllocation(
        backend_id=str(backend_id),
        state=GPUAllocationState.RESIDENT,
        budget_mb=budget_mb,
        generation=generation,
        eviction_priority=eviction_priority,
        evictable=True,
        max_concurrency=4,
        reservation_lease_id=None,
        reservation_owner_id=None,
        last_used_at_ms=last_used_at_ms,
        not_evict_before_ms=not_evict_before_ms,
    )


def _card_snapshot(
    resource_id: str,
    *,
    allocatable_mb: int,
    allocations: tuple[GPUAllocation, ...],
    transition_present: bool = False,
    observed_at_ms: int = 1,
    leases: tuple[GPURequestLease, ...] = (),
) -> GPUCardSnapshot:
    return GPUCardSnapshot(
        resource_id=resource_id,
        observed_at_ms=observed_at_ms,
        allocatable_mb=allocatable_mb,
        ready=True,
        reconcile_deadline_ms=0,
        ledger_revision=1,
        ledger_incarnation="test-incarnation",
        committed_mb=sum(item.budget_mb for item in allocations if item.counted),
        backend_ids=tuple(item.backend_id for item in allocations),
        active_backend_ids=tuple(item.backend_id for item in allocations),
        backend_memberships=(),
        allocations=allocations,
        leases=leases,
        not_ready_reason=None,
        card_queue_count=0,
        backend_queue_count=0,
        transition_present=transition_present,
    )


def _workload_lease(
    allocation: GPUAllocation,
    *,
    lease_id: str = "victim-lease",
) -> GPURequestLease:
    assert allocation.generation is not None
    return GPURequestLease(
        lease_id=lease_id,
        backend_id=allocation.backend_id,
        owner_id="victim-owner",
        generation=allocation.generation,
        operation="predict",
        state=GPURequestLeaseState.ACTIVE,
        created_at_ms=1,
        heartbeat_deadline_ms=10_000,
        hard_deadline_ms=20_000,
    )


def _idle_eviction_subject(
    allocation: GPUAllocation,
    resource_id: str,
    *,
    challenge: str,
) -> GPUIdleEvictionRuntimeSubject:
    backend_id = uuid.UUID(allocation.backend_id)
    backend = MLBackendRegistry(
        id=backend_id,
        name=f"victim-{backend_id}",
        url=f"http://victim-{backend_id}.test",
        state="connected",
        auth_method="none",
        auth_token=None,
        extra_params={"gpu_max_concurrency": allocation.max_concurrency},
        gpu_resource_id=resource_id,
        vram_budget_mb=allocation.budget_mb,
        eviction_priority=allocation.eviction_priority,
    )
    assert allocation.generation is not None
    return GPUIdleEvictionRuntimeSubject(
        backend=backend,
        backend_registry_id=backend_id,
        gpu_resource_id=resource_id,
        membership_epoch=1,
        budget_mb=allocation.budget_mb,
        eviction_priority=allocation.eviction_priority,
        max_concurrency=allocation.max_concurrency,
        boot_id=f"boot-{backend_id}",
        generation=allocation.generation,
        generation_high_water=int(allocation.generation),
        pool_ids=("models",),
        control_epoch="5",
        runtime_epoch="2",
        challenge=challenge,
        require_idle=True,
        db_now=datetime.now(UTC),
    )


def _prepared_idle_eviction_subject(
    subject: GPUIdleEvictionRuntimeSubject,
    *,
    token_expires_at: datetime,
) -> GPUPreparedIdleEvictionRuntimeSubject:
    return GPUPreparedIdleEvictionRuntimeSubject(
        backend=subject.backend,
        backend_registry_id=subject.backend_registry_id,
        gpu_resource_id=subject.gpu_resource_id,
        membership_epoch=subject.membership_epoch,
        budget_mb=subject.budget_mb,
        eviction_priority=subject.eviction_priority,
        max_concurrency=subject.max_concurrency,
        boot_id=subject.boot_id,
        source_generation=subject.generation,
        generation=str(int(subject.generation) + 1),
        pool_ids=subject.pool_ids,
        control_epoch=subject.control_epoch,
        runtime_epoch=subject.runtime_epoch,
        token_expires_at=token_expires_at,
        require_idle=subject.require_idle,
        db_now=subject.db_now,
    )


def _prepared_eviction_cancel_subject() -> GPUPreparedEvictionCancelRuntimeSubject:
    allocation = _allocation(
        uuid.uuid4(),
        budget_mb=2048,
        generation="7",
    )
    source = replace(
        _idle_eviction_subject(allocation, _RESOURCE_ID, challenge="a" * 64),
        require_idle=False,
    )
    drain = _prepared_idle_eviction_subject(
        source,
        token_expires_at=source.db_now + timedelta(seconds=60),
    )
    return GPUPreparedEvictionCancelRuntimeSubject(
        backend=drain.backend,
        backend_registry_id=drain.backend_registry_id,
        gpu_resource_id=drain.gpu_resource_id,
        membership_epoch=drain.membership_epoch,
        budget_mb=drain.budget_mb,
        eviction_priority=drain.eviction_priority,
        max_concurrency=drain.max_concurrency,
        boot_id=drain.boot_id,
        source_generation=drain.source_generation,
        drain_generation=drain.generation,
        generation=str(int(drain.generation) + 1),
        pool_ids=drain.pool_ids,
        control_epoch=drain.control_epoch,
        runtime_epoch=drain.runtime_epoch,
        owner_id="evict:cancel-owner",
        operation="evict",
        owner_hard_deadline_ms=int(
            (source.db_now + timedelta(seconds=90)).timestamp() * 1000
        ),
        drain_token_expires_at=drain.token_expires_at,
        token_expires_at=source.db_now + timedelta(seconds=80),
        jti="transition:durable-cancel-jti",
        idempotent=False,
        db_now=source.db_now,
    )


def _cancel_response(
    subject: GPUPreparedEvictionCancelRuntimeSubject,
    *,
    generation: str | None = None,
    state: str = "resident",
    gpu_loaded: bool | None = True,
    draining: bool = False,
    evictable: bool = True,
    boot_id: str | None = None,
    control_epoch: str | None = None,
    backend_id: str | None = None,
    resource_id: str | None = None,
    pools: dict | None = None,
) -> DrainTransitionResponse:
    response_generation = generation or subject.generation
    return DrainTransitionResponse(
        generation=response_generation,
        draining=draining,
        active_requests=2,
        ready_to_unload=False,
        residency={
            "state": state,
            "gpu_loaded": gpu_loaded,
            "active_requests": 2,
            "builders": 1,
            "borrowers": 1,
            "draining": draining,
            "evictable": evictable,
            "generation": response_generation,
            "pools": pools
            or {
                pool_id: {
                    "resident": True,
                    "device": "cuda:0",
                    "provider": "CUDAExecutionProvider",
                }
                for pool_id in subject.pool_ids
            },
            "boot_id": boot_id or subject.boot_id,
            "lifecycle_gate": "enforce",
            "control_epoch": control_epoch or subject.control_epoch,
            "identity": {
                "backend_registry_id": backend_id or str(subject.backend_registry_id),
                "gpu_resource_id": resource_id or subject.gpu_resource_id,
            },
        },
    )


def _drain_response(
    subject: GPUPreparedIdleEvictionRuntimeSubject,
) -> DrainTransitionResponse:
    return DrainTransitionResponse(
        generation=subject.generation,
        draining=True,
        active_requests=0,
        ready_to_unload=True,
        residency={
            "state": "draining",
            "gpu_loaded": True,
            "active_requests": 0,
            "builders": 0,
            "borrowers": 0,
            "draining": True,
            "evictable": False,
            "generation": subject.generation,
            "pools": {
                "default": {
                    "resident": True,
                    "device": "cuda:0",
                    "provider": "CUDAExecutionProvider",
                }
            },
            "boot_id": subject.boot_id,
            "lifecycle_gate": "enforce",
            "control_epoch": subject.control_epoch,
            "identity": {
                "backend_registry_id": str(subject.backend_registry_id),
                "gpu_resource_id": subject.gpu_resource_id,
            },
        },
    )


def _busy_drain_response(
    subject: GPUPreparedIdleEvictionRuntimeSubject,
    *,
    active_requests: int = 1,
    builders: int = 0,
    borrowers: int = 0,
) -> DrainTransitionResponse:
    ready = active_requests == 0 and builders == 0 and borrowers == 0
    return DrainTransitionResponse(
        generation=subject.generation,
        draining=True,
        active_requests=active_requests,
        ready_to_unload=ready,
        residency={
            "state": "draining",
            "gpu_loaded": True,
            "active_requests": active_requests,
            "builders": builders,
            "borrowers": borrowers,
            "draining": True,
            "evictable": False,
            "generation": subject.generation,
            "pools": {
                pool_id: {
                    "resident": True,
                    "device": "cuda:0",
                    "provider": "CUDAExecutionProvider",
                }
                for pool_id in subject.pool_ids
            },
            "boot_id": subject.boot_id,
            "lifecycle_gate": "enforce",
            "control_epoch": subject.control_epoch,
            "identity": {
                "backend_registry_id": str(subject.backend_registry_id),
                "gpu_resource_id": subject.gpu_resource_id,
            },
        },
    )


def _unload_response(
    subject: GPUPreparedIdleEvictionRuntimeSubject,
) -> ManagedUnloadResponse:
    return ManagedUnloadResponse(
        generation=subject.generation,
        unloaded=True,
        unloaded_count=1,
        residency={
            "state": "unloaded",
            "gpu_loaded": False,
            "active_requests": 0,
            "builders": 0,
            "borrowers": 0,
            "draining": False,
            "evictable": False,
            "generation": subject.generation,
            "pools": {
                "default": {
                    "resident": False,
                    "device": None,
                    "provider": None,
                }
            },
            "boot_id": subject.boot_id,
            "lifecycle_gate": "enforce",
            "control_epoch": subject.control_epoch,
            "identity": {
                "backend_registry_id": str(subject.backend_registry_id),
                "gpu_resource_id": subject.gpu_resource_id,
            },
        },
    )


def _request(
    subject: GPUResidentRuntimeSubject | GPUColdRuntimeSubject,
    *,
    operation: str = "predict",
    scope: AdmissionScope = AdmissionScope.PREDICT,
) -> GPUDispatchRequest:
    return GPUDispatchRequest(
        backend_id=str(subject.backend_registry_id),
        gpu_resource_id=subject.gpu_resource_id,
        operation=operation,  # type: ignore[arg-type]
        scope=scope,
    )


class _RecordingSigner:
    def __init__(
        self,
        events: list[str],
        *,
        fail: bool = False,
        token: str = "signed-workload-token",
    ) -> None:
        self.events = events
        self.fail = fail
        self.token = token
        self.claims: AdmissionTokenClaims | None = None
        self.claims_history: list[AdmissionTokenClaims] = []

    def sign(self, claims: AdmissionTokenClaims) -> str:
        self.events.append("sign")
        self.claims = claims
        self.claims_history.append(claims)
        if self.fail:
            raise RuntimeError("signing failed")
        return self.token


class _ScopeSigner(_RecordingSigner):
    def sign(self, claims: AdmissionTokenClaims) -> str:
        super().sign(claims)
        return f"signed:{claims.scope.value}:{claims.jti}"


def test_eviction_cancel_signer_replays_exact_durable_resume_claims() -> None:
    events: list[str] = []
    signer = _ScopeSigner(events)
    subject = _prepared_eviction_cancel_subject()

    first = authority_module._sign_eviction_cancel_grant(signer, subject)
    replayed = authority_module._sign_eviction_cancel_grant(signer, subject)

    assert first == replayed == f"signed:resume:{subject.jti}"
    assert signer.claims_history[0] == signer.claims_history[1]
    claims = signer.claims_history[0]
    assert claims.scope is AdmissionScope.RESUME
    assert claims.generation == subject.generation
    assert claims.jti == subject.jti
    assert claims.owner == subject.owner_id
    assert claims.operation == subject.operation == "evict"
    assert claims.boot_id == subject.boot_id
    assert claims.control_epoch == subject.control_epoch
    assert claims.exp == min(
        int(subject.token_expires_at.timestamp()),
        subject.owner_hard_deadline_ms // 1000,
    )


def test_eviction_cancel_ack_requires_exact_resident_gpu_identity_and_pools() -> None:
    subject = _prepared_eviction_cancel_subject()
    valid = _cancel_response(subject)

    assert valid.residency.active_requests == 2
    assert valid.residency.builders == 1
    assert valid.residency.borrowers == 1
    assert authority_module._eviction_cancel_ack_matches(subject, valid)
    assert not authority_module._eviction_cancel_ack_matches(
        subject,
        valid.model_copy(update={"ok": False}),
    )

    assert not authority_module._eviction_cancel_ack_matches(
        subject,
        _cancel_response(subject, generation=str(int(subject.generation) + 1)),
    )
    assert not authority_module._eviction_cancel_ack_matches(
        subject,
        _cancel_response(
            subject,
            state="draining",
            draining=True,
            evictable=False,
        ),
    )

    assert not authority_module._eviction_cancel_ack_matches(
        subject,
        _cancel_response(subject, gpu_loaded=None, evictable=False),
    )
    assert not authority_module._eviction_cancel_ack_matches(
        subject,
        _cancel_response(subject, evictable=False),
    )
    assert not authority_module._eviction_cancel_ack_matches(
        subject,
        _cancel_response(subject, boot_id="other-boot"),
    )
    assert not authority_module._eviction_cancel_ack_matches(
        subject,
        _cancel_response(subject, control_epoch="99"),
    )
    assert not authority_module._eviction_cancel_ack_matches(
        subject,
        _cancel_response(subject, backend_id=str(uuid.uuid4())),
    )
    assert not authority_module._eviction_cancel_ack_matches(
        subject,
        _cancel_response(subject, resource_id="other-node/index:0"),
    )
    assert not authority_module._eviction_cancel_ack_matches(
        subject,
        _cancel_response(
            subject,
            pools={
                "unexpected": {
                    "resident": True,
                    "device": "cuda:0",
                    "provider": None,
                }
            },
        ),
    )
    assert not authority_module._eviction_cancel_ack_matches(
        subject,
        _cancel_response(
            subject,
            pools={
                subject.pool_ids[0]: {
                    "resident": None,
                    "device": None,
                    "provider": None,
                }
            },
        ),
    )
    assert not authority_module._eviction_cancel_ack_matches(
        subject,
        _cancel_response(
            subject,
            pools={
                subject.pool_ids[0]: {
                    "resident": False,
                    "device": "cpu",
                    "provider": "CPUExecutionProvider",
                }
            },
        ),
    )


@pytest.mark.parametrize(
    ("active_requests", "builders", "borrowers"),
    ((2, 0, 0), (0, 1, 0), (0, 0, 1), (0, 0, 0)),
)
def test_busy_drain_ack_accepts_activity_but_requires_exact_draining_identity(
    active_requests: int,
    builders: int,
    borrowers: int,
) -> None:
    subject = _prepared_idle_eviction_subject(
        replace(
            _idle_eviction_subject(
                _allocation(uuid.uuid4(), budget_mb=2048, generation="7"),
                _RESOURCE_ID,
                challenge="a" * 64,
            ),
            require_idle=False,
        ),
        token_expires_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    valid = _busy_drain_response(
        subject,
        active_requests=active_requests,
        builders=builders,
        borrowers=borrowers,
    )

    assert authority_module._busy_drain_ack_matches(subject, valid)
    assert not authority_module._busy_drain_ack_matches(
        subject,
        valid.model_copy(update={"ok": False}),
    )
    assert not authority_module._busy_drain_ack_matches(
        subject,
        valid.model_copy(
            update={
                "residency": valid.residency.model_copy(
                    update={
                        "pools": {
                            "unexpected": next(iter(valid.residency.pools.values()))
                        }
                    }
                )
            }
        ),
    )


def test_busy_drain_ack_rejects_generation_identity_and_counter_drift() -> None:
    subject = _prepared_idle_eviction_subject(
        replace(
            _idle_eviction_subject(
                _allocation(uuid.uuid4(), budget_mb=2048, generation="7"),
                _RESOURCE_ID,
                challenge="a" * 64,
            ),
            require_idle=False,
        ),
        token_expires_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    valid = _busy_drain_response(subject, active_requests=2)

    assert not authority_module._busy_drain_ack_matches(
        subject,
        valid.model_copy(update={"generation": "99"}),
    )
    assert not authority_module._busy_drain_ack_matches(
        subject,
        valid.model_copy(update={"active_requests": 1}),
    )
    for residency_update in (
        {"generation": "99"},
        {"boot_id": "other-boot"},
        {"control_epoch": "99"},
        {
            "identity": valid.residency.identity.model_copy(
                update={"gpu_resource_id": "other-node/index:0"}
            )
        },
    ):
        assert not authority_module._busy_drain_ack_matches(
            subject,
            valid.model_copy(
                update={
                    "residency": valid.residency.model_copy(
                        update=residency_update,
                    )
                }
            ),
        )


class _RecordingStore:
    def __init__(
        self,
        events: list[str],
        *,
        admission_status: str = "admitted",
        heartbeat_status: str = "heartbeated",
        cold_owner_status: str = "acquired",
        cold_revalidate_status: str = "renewed",
        admission_statuses: list[str] | None = None,
        card_queue_positions: list[int] | None = None,
    ) -> None:
        self.events = events
        self.admission_status = admission_status
        self.heartbeat_status = heartbeat_status
        self.cold_owner_status = cold_owner_status
        self.cold_revalidate_status = cold_revalidate_status
        self.admission_statuses = list(admission_statuses or [])
        self.card_queue_positions = list(card_queue_positions or [1])
        self.admit_attempts = 0
        self.admit_kwargs: dict | None = None
        self.admit_kwargs_history: list[dict] = []
        self.backend_enqueue_kwargs: list[dict] = []
        self.card_enqueue_kwargs: list[dict] = []
        self.queue_position_kwargs: list[dict] = []
        self.queue_cancel_kwargs: list[dict] = []
        self.heartbeat_kwargs: list[dict] = []
        self.release_kwargs: list[dict] = []
        self.uncertain_kwargs: list[dict] = []
        self.transition_kwargs: list[dict] = []
        self.finalize_kwargs: list[dict] = []
        self.cold_owner_kwargs: list[dict] = []
        self.cold_revalidate_kwargs: list[dict] = []
        self.cold_release_kwargs: list[dict] = []
        self.hard_deadline_ms: int | None = None
        self.uncertain_entered = asyncio.Event()
        self.uncertain_release = asyncio.Event()
        self.block_uncertain = False

    async def ping(self) -> bool:
        self.events.append("ping")
        return True

    async def enqueue_backend(self, resource_id: str, **kwargs):
        self.events.append("backend_enqueue")
        self.backend_enqueue_kwargs.append({"resource_id": resource_id, **kwargs})
        return GPUQueueResult(
            status="queued",
            ticket_id=kwargs["ticket_id"],
            position=1,
            expires_at_ms=int(time.time() * 1000) + kwargs["ttl_ms"],
        )

    async def enqueue_card(self, resource_id: str, **kwargs):
        self.events.append("card_enqueue")
        self.card_enqueue_kwargs.append({"resource_id": resource_id, **kwargs})
        return GPUQueueResult(
            status="queued",
            ticket_id=kwargs["ticket_id"],
            position=self.card_queue_positions[0],
            expires_at_ms=int(time.time() * 1000) + kwargs["ttl_ms"],
        )

    async def queue_position(self, resource_id: str, **kwargs):
        position = self.card_queue_positions[0]
        if len(self.card_queue_positions) > 1:
            self.card_queue_positions.pop(0)
        self.events.append(f"card_position:{position}")
        self.queue_position_kwargs.append({"resource_id": resource_id, **kwargs})
        return GPUQueueResult(
            status="queued",
            ticket_id=kwargs["ticket_id"],
            position=position,
            expires_at_ms=int(time.time() * 1000) + 30_000,
        )

    async def cancel_queue_ticket(self, resource_id: str, **kwargs):
        kind = "card" if kwargs["card_queue"] else "backend"
        self.events.append(f"{kind}_cancel")
        self.queue_cancel_kwargs.append({"resource_id": resource_id, **kwargs})
        return GPUQueueResult(status="cancelled", ticket_id=kwargs["ticket_id"])

    async def snapshot(self, resource_id: str) -> GPUCardSnapshot:
        return GPUCardSnapshot(
            resource_id=resource_id,
            observed_at_ms=1,
            allocatable_mb=8192,
            ready=True,
            reconcile_deadline_ms=0,
            ledger_revision=1,
            ledger_incarnation="test-incarnation",
            committed_mb=0,
            backend_ids=(),
            active_backend_ids=(),
            backend_memberships=(),
            allocations=(),
            leases=(),
            not_ready_reason=None,
            card_queue_count=0,
            backend_queue_count=0,
            transition_present=False,
        )

    async def acquire_cold_admission_owner(self, resource_id: str, **kwargs):
        self.events.append("cold_owner")
        self.cold_owner_kwargs.append({"resource_id": resource_id, **kwargs})
        return GPUTransitionOwnerResult(
            status=self.cold_owner_status,  # type: ignore[arg-type]
            owner_id=kwargs["owner_id"],
            generation=kwargs["generation"],
        )

    async def revalidate_cold_admission_owner(self, resource_id: str, **kwargs):
        self.events.append("cold_revalidate")
        self.cold_revalidate_kwargs.append({"resource_id": resource_id, **kwargs})
        return GPUTransitionOwnerResult(
            status=self.cold_revalidate_status,  # type: ignore[arg-type]
            owner_id=kwargs["owner_id"],
            generation=kwargs["generation"],
        )

    async def release_cold_admission_owner(self, resource_id: str, **kwargs):
        self.events.append("cold_release")
        self.cold_release_kwargs.append({"resource_id": resource_id, **kwargs})
        return GPUTransitionOwnerResult(status="released")

    async def admit(self, resource_id: str, **kwargs) -> GPUAdmissionResult:
        self.events.append("admit")
        self.admit_attempts += 1
        self.admit_kwargs = {"resource_id": resource_id, **kwargs}
        self.admit_kwargs_history.append(self.admit_kwargs)
        admission_status = (
            self.admission_statuses.pop(0)
            if self.admission_statuses
            else self.admission_status
        )
        if admission_status == "error" or (
            admission_status == "error_once" and self.admit_attempts == 1
        ):
            raise RuntimeError("admission response lost")
        now_ms = int(time.time() * 1000)
        if admission_status in {"admitted", "error_once"}:
            self.hard_deadline_ms = now_ms + 120_000
            return GPUAdmissionResult(
                status="admitted",
                reason=(
                    "cold_reservation"
                    if kwargs.get("require_cold_owner")
                    else "resident_fast_path"
                ),
                committed_mb=4096,
                lease_count=1,
                allocation_state=(
                    GPUAllocationState.RESERVING
                    if kwargs.get("require_cold_owner")
                    else GPUAllocationState.RESIDENT
                ),
                heartbeat_deadline_ms=now_ms + 15_000,
                hard_deadline_ms=self.hard_deadline_ms,
            )
        return GPUAdmissionResult(
            status=admission_status,  # type: ignore[arg-type]
            reason="rejected",
            committed_mb=4096,
            lease_count=4,
        )

    async def heartbeat_lease(self, resource_id: str, **kwargs):
        self.events.append("heartbeat")
        self.heartbeat_kwargs.append({"resource_id": resource_id, **kwargs})
        return GPULeaseMutationResult(
            status=self.heartbeat_status,  # type: ignore[arg-type]
        )

    async def release_lease(self, resource_id: str, **kwargs):
        self.events.append("release")
        self.release_kwargs.append({"resource_id": resource_id, **kwargs})
        return GPULeaseMutationResult(status="released")

    async def mark_lease_uncertain(self, resource_id: str, **kwargs):
        self.events.append("uncertain")
        self.uncertain_kwargs.append({"resource_id": resource_id, **kwargs})
        self.uncertain_entered.set()
        if self.block_uncertain:
            await self.uncertain_release.wait()
        return GPULeaseMutationResult(status="uncertain")

    async def transition_allocation(self, resource_id: str, **kwargs):
        target_state = kwargs["target_state"]
        self.events.append(f"transition:{target_state.value}")
        self.transition_kwargs.append({"resource_id": resource_id, **kwargs})
        return GPUTransitionResult(
            status="transitioned",
            state=target_state,
            generation=kwargs["expected_generation"],
            committed_mb=4096,
        )

    async def finalize_cold_allocation(self, resource_id: str, **kwargs):
        target_state = kwargs["target_state"]
        self.events.append(f"finalize:{target_state.value}")
        self.finalize_kwargs.append({"resource_id": resource_id, **kwargs})
        return GPUTransitionResult(
            status="transitioned",
            state=target_state,
            generation=kwargs["expected_generation"],
            committed_mb=(
                4096
                if target_state
                in {GPUAllocationState.RESIDENT, GPUAllocationState.UNKNOWN}
                else 0
            ),
        )

    async def aclose(self) -> None:
        self.events.append("close")


class _EvictionStore(_RecordingStore):
    def __init__(
        self,
        events: list[str],
        snapshots: list[GPUCardSnapshot],
    ) -> None:
        super().__init__(events)
        self.snapshots = snapshots
        self.victim_budgets = {
            item.backend_id: item.budget_mb
            for snapshot in snapshots
            for item in snapshot.allocations
        }
        self.begin_kwargs: list[dict] = []
        self.eviction_heartbeat_kwargs: list[dict] = []
        self.eviction_release_kwargs: list[dict] = []

    async def snapshot(self, resource_id: str) -> GPUCardSnapshot:
        self.events.append(f"snapshot:{resource_id}")
        snapshot = self.snapshots[0]
        assert snapshot.resource_id == resource_id
        if len(self.snapshots) > 1:
            self.snapshots.pop(0)
        return snapshot

    async def begin_idle_eviction(self, resource_id: str, **kwargs):
        victim_id = kwargs["victim_backend_id"]
        self.events.append(f"eviction_begin:{victim_id}")
        self.begin_kwargs.append({"resource_id": resource_id, **kwargs})
        now_ms = int(time.time() * 1000)
        return GPUIdleEvictionResult(
            status="selected",
            reason="idle_victim_selected",
            committed_mb=0,
            shortfall_mb=0,
            victim_backend_id=victim_id,
            victim_generation=kwargs["victim_next_generation"],
            victim_budget_mb=self.victim_budgets[victim_id],
            owner_id=kwargs["owner_id"],
            owner_expires_at_ms=now_ms + kwargs["ttl_ms"],
            owner_hard_deadline_ms=now_ms + kwargs["hard_ttl_ms"],
        )

    async def heartbeat_transition_owner(self, resource_id: str, **kwargs):
        backend_id = kwargs["backend_id"]
        self.events.append(f"eviction_heartbeat:{backend_id}")
        self.eviction_heartbeat_kwargs.append({"resource_id": resource_id, **kwargs})
        return GPUTransitionOwnerResult(
            status="renewed",
            owner_id=kwargs["owner_id"],
            generation=kwargs["generation"],
        )

    async def release_transition_owner(self, resource_id: str, **kwargs):
        backend_id = kwargs["backend_id"]
        self.events.append(f"eviction_release:{backend_id}")
        self.eviction_release_kwargs.append({"resource_id": resource_id, **kwargs})
        return GPUTransitionOwnerResult(status="released")


def _install_authority_fakes(
    monkeypatch,
    events: list[str],
    subject: GPUResidentRuntimeSubject,
) -> None:
    async def read_subject(db, *, backend_id: str, gpu_resource_id: str):
        events.append("subject")
        assert backend_id == str(subject.backend_registry_id)
        assert gpu_resource_id == subject.gpu_resource_id
        return subject

    async def record_horizon(
        session_factory,
        expected_subject,
        *,
        token_expires_at: datetime,
    ) -> datetime:
        events.append("horizon")
        assert expected_subject is subject
        return token_expires_at

    monkeypatch.setattr(
        authority_module,
        "read_gpu_resident_runtime_subject",
        read_subject,
    )
    monkeypatch.setattr(
        authority_module,
        "record_gpu_resident_runtime_token_expiry",
        record_horizon,
    )


def _install_cold_authority_fakes(
    monkeypatch,
    events: list[str],
    subject: GPUColdRuntimeSubject,
    *,
    prepared: GPUPreparedColdRuntimeSubject | None = None,
    terminal_state: GPUAllocationState = GPUAllocationState.RESIDENT,
    terminal_status: str = "finalized",
    cold_read_challenges: list[str | None] | None = None,
    refreshed_subject: GPUColdRuntimeSubject | None = None,
) -> GPUPreparedColdRuntimeSubject:
    prepared_subject = prepared or _prepared_cold_subject(subject)
    last_prepared_subject = prepared_subject

    async def reject_resident(db, *, backend_id: str, gpu_resource_id: str):
        events.append("subject")
        assert backend_id == str(subject.backend_registry_id)
        assert gpu_resource_id == subject.gpu_resource_id
        raise GPUResidentRuntimeSubjectError("resident_runtime_not_ready")

    async def read_cold(
        db,
        *,
        backend_id: str,
        gpu_resource_id: str,
        expected_challenge: str | None = None,
    ):
        events.append("cold_subject")
        if cold_read_challenges is not None:
            cold_read_challenges.append(expected_challenge)
        assert backend_id == str(subject.backend_registry_id)
        assert gpu_resource_id == subject.gpu_resource_id
        if expected_challenge is not None:
            assert len(expected_challenge) == 64
            return refreshed_subject or subject
        return subject

    async def prepare_cold(
        session_factory,
        expected_subject,
        *,
        token_expires_at: datetime,
    ):
        nonlocal last_prepared_subject
        events.append("prepare")
        assert expected_subject in {subject, refreshed_subject}
        assert token_expires_at > subject.db_now
        base = (
            prepared_subject
            if expected_subject is subject
            else _prepared_cold_subject(expected_subject)
        )
        last_prepared_subject = replace(
            base,
            token_expires_at=token_expires_at,
        )
        return last_prepared_subject

    async def refresh_terminal_health(session_factory, expected_subject):
        events.append("health")
        assert expected_subject.backend_registry_id == subject.backend_registry_id
        return "a" * 64

    async def commit_terminal(
        session_factory,
        store,
        expected_subject,
        *,
        challenge: str | None,
        lease_id: str,
        owner_id: str,
        resident_cooldown_ms: int,
    ):
        committed_state = (
            terminal_state if challenge is not None else GPUAllocationState.UNKNOWN
        )
        events.append(f"terminal:{committed_state.value}")
        assert expected_subject.generation == last_prepared_subject.generation
        assert challenge in {None, "a" * 64}
        assert store.admit_kwargs is not None
        assert lease_id == store.admit_kwargs["lease_id"]
        assert owner_id == store.admit_kwargs["owner_id"]
        assert resident_cooldown_ms == 30_000
        return GPUColdTerminalCommitResult(
            status=terminal_status,  # type: ignore[arg-type]
            state=committed_state,
            reason=committed_state.value,
        )

    monkeypatch.setattr(
        authority_module,
        "read_gpu_resident_runtime_subject",
        reject_resident,
    )
    monkeypatch.setattr(
        authority_module,
        "read_gpu_cold_runtime_subject",
        read_cold,
    )
    monkeypatch.setattr(
        authority_module,
        "prepare_gpu_cold_runtime_generation",
        prepare_cold,
    )
    monkeypatch.setattr(
        authority_module,
        "_refresh_cold_terminal_health",
        refresh_terminal_health,
    )
    monkeypatch.setattr(
        authority_module,
        "commit_gpu_cold_terminal_from_health",
        commit_terminal,
    )
    return prepared_subject


def _install_eviction_authority_fakes(
    monkeypatch,
    events: list[str],
    allocations: tuple[GPUAllocation, ...],
    resource_id: str,
    *,
    proof_challenges: dict[tuple[uuid.UUID, str], str | None] | None = None,
) -> dict[uuid.UUID, GPUPreparedIdleEvictionRuntimeSubject]:
    allocation_by_id = {
        uuid.UUID(allocation.backend_id): allocation for allocation in allocations
    }
    prepared_by_id: dict[uuid.UUID, GPUPreparedIdleEvictionRuntimeSubject] = {}

    async def read_idle(
        db,
        *,
        backend_id: str,
        gpu_resource_id: str,
        expected_generation: str,
        challenge: str,
    ) -> GPUIdleEvictionRuntimeSubject:
        victim_id = uuid.UUID(backend_id)
        allocation = allocation_by_id[victim_id]
        events.append(f"idle_subject:{backend_id}")
        assert gpu_resource_id == resource_id
        assert expected_generation == allocation.generation
        if proof_challenges is not None:
            proof_challenges[(victim_id, "initial")] = challenge
        return _idle_eviction_subject(
            allocation,
            resource_id,
            challenge=challenge,
        )

    async def prepare_idle(
        session_factory,
        expected_subject: GPUIdleEvictionRuntimeSubject,
        *,
        token_expires_at: datetime,
    ) -> GPUPreparedIdleEvictionRuntimeSubject:
        backend_id = expected_subject.backend_registry_id
        events.append(f"prepare_eviction:{backend_id}")
        prepared = _prepared_idle_eviction_subject(
            expected_subject,
            token_expires_at=token_expires_at,
        )
        prepared_by_id[backend_id] = prepared
        return prepared

    async def commit_phase(
        session_factory,
        store,
        expected_subject: GPUPreparedIdleEvictionRuntimeSubject,
        *,
        phase: str,
        challenge: str | None,
        owner_id: str,
    ) -> GPUEvictionCommitResult:
        backend_id = expected_subject.backend_registry_id
        events.append(f"commit_{phase}:{backend_id}")
        assert challenge is not None
        if proof_challenges is not None:
            proof_challenges[(backend_id, phase)] = challenge
        assert owner_id.startswith("evict:")
        state = (
            GPUAllocationState.UNLOADING
            if phase == "drain"
            else GPUAllocationState.UNLOADED
        )
        return GPUEvictionCommitResult(
            status="finalized",
            state=state,
            reason=state.value,
        )

    class SuccessfulEvictionClient:
        def __init__(
            self, backend: MLBackendRegistry, *, shadow_session_factory=None
        ) -> None:
            self.backend_id = backend.id

        async def lifecycle_drain(self, grant):
            events.append(f"drain:{self.backend_id}")
            grant.report_response(200)
            return _drain_response(prepared_by_id[self.backend_id])

        async def lifecycle_unload(self, grant):
            events.append(f"unload:{self.backend_id}")
            grant.report_response(200)
            return _unload_response(prepared_by_id[self.backend_id])

    monkeypatch.setattr(
        authority_module,
        "read_gpu_idle_eviction_runtime_subject",
        read_idle,
    )
    monkeypatch.setattr(
        authority_module,
        "prepare_gpu_idle_eviction_runtime_generation",
        prepare_idle,
    )
    monkeypatch.setattr(
        authority_module,
        "commit_gpu_eviction_phase_from_health",
        commit_phase,
    )
    monkeypatch.setattr(
        authority_module,
        "MLBackendClient",
        SuccessfulEvictionClient,
    )
    return prepared_by_id


def _session_factory(events: list[str]):
    @asynccontextmanager
    async def sessions():
        events.append("db")
        yield object()

    return sessions


@pytest.mark.parametrize("ready_order", ("redis_first", "backend_first"))
@pytest.mark.asyncio
async def test_busy_eviction_wait_requires_redis_and_backend_domains(
    monkeypatch,
    ready_order: str,
) -> None:
    events: list[str] = []
    victim = _allocation(uuid.UUID(int=51), budget_mb=4096, generation="7")
    source = replace(
        _idle_eviction_subject(victim, _RESOURCE_ID, challenge="a" * 64),
        require_idle=False,
    )
    prepared = _prepared_idle_eviction_subject(
        source,
        token_expires_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    draining = replace(
        victim,
        state=GPUAllocationState.DRAINING,
        generation=prepared.generation,
    )
    lease = _workload_lease(victim)
    if ready_order == "redis_first":
        snapshots = [
            _card_snapshot(
                _RESOURCE_ID,
                allocatable_mb=4096,
                allocations=(draining,),
                transition_present=True,
            ),
            _card_snapshot(
                _RESOURCE_ID,
                allocatable_mb=4096,
                allocations=(draining,),
                transition_present=True,
            ),
        ]
        health_statuses = ["draining_busy", "ready_to_unload"]
    else:
        snapshots = [
            _card_snapshot(
                _RESOURCE_ID,
                allocatable_mb=4096,
                allocations=(draining,),
                transition_present=True,
                leases=(lease,),
            ),
            _card_snapshot(
                _RESOURCE_ID,
                allocatable_mb=4096,
                allocations=(draining,),
                transition_present=True,
            ),
        ]
        health_statuses = ["ready_to_unload", "ready_to_unload"]
    store = _EvictionStore(events, snapshots)
    challenges: list[str] = []

    async def refresh_health(backend_id: uuid.UUID, challenge: str) -> bool:
        assert backend_id == prepared.backend_registry_id
        challenges.append(challenge)
        return True

    async def read_health(
        session_factory,
        subject,
        *,
        challenge: str,
    ) -> GPUEvictionDrainHealth:
        assert subject is prepared
        assert challenge == challenges[-1]
        status = health_statuses.pop(0)
        return GPUEvictionDrainHealth(status=status, reason=status)

    monkeypatch.setattr(
        authority_module,
        "_read_busy_eviction_drain_health",
        read_health,
    )
    challenge = await authority_module._wait_for_busy_eviction_ready(
        _session_factory(events),
        store,  # type: ignore[arg-type]
        prepared,
        health_refresher=refresh_health,
        heartbeat_task=None,
        queue_deadline=time.monotonic() + 10,
        ticket_expires_at_ms=int(time.time() * 1000) + 10_000,
        work_hard_deadline_ms=int(time.time() * 1000) + 10_000,
        poll_interval_seconds=0.001,
    )

    assert challenge == challenges[-1]
    assert len(challenges) == 2
    assert health_statuses == []


@pytest.mark.asyncio
async def test_busy_eviction_wait_is_bounded_by_work_deadline(monkeypatch) -> None:
    events: list[str] = []
    victim = _allocation(uuid.UUID(int=52), budget_mb=4096, generation="7")
    prepared = _prepared_idle_eviction_subject(
        replace(
            _idle_eviction_subject(victim, _RESOURCE_ID, challenge="a" * 64),
            require_idle=False,
        ),
        token_expires_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    draining = replace(
        victim,
        state=GPUAllocationState.DRAINING,
        generation=prepared.generation,
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                _RESOURCE_ID,
                allocatable_mb=4096,
                allocations=(draining,),
                transition_present=True,
            )
        ],
    )

    async def blocked_health(backend_id: uuid.UUID, challenge: str) -> bool:
        await asyncio.Event().wait()
        return True

    started = time.monotonic()
    with pytest.raises(GPUArbiterDispatchError) as caught:
        await authority_module._wait_for_busy_eviction_ready(
            _session_factory(events),
            store,  # type: ignore[arg-type]
            prepared,
            health_refresher=blocked_health,
            heartbeat_task=None,
            queue_deadline=time.monotonic() + 10,
            ticket_expires_at_ms=int(time.time() * 1000) + 10_000,
            work_hard_deadline_ms=int(time.time() * 1000) + 20,
            poll_interval_seconds=1,
        )

    assert caught.value.error_code == GPUArbiterErrorCode.CAPACITY_UNAVAILABLE.value
    assert time.monotonic() - started < 1


@pytest.mark.asyncio
async def test_busy_eviction_owner_keeps_full_cancel_horizon(monkeypatch) -> None:
    events: list[str] = []
    requester = replace(_cold_subject(), budget_mb=4096)
    victim = _allocation(uuid.UUID(int=53), budget_mb=4096, generation="7")
    source = replace(
        _idle_eviction_subject(victim, requester.gpu_resource_id, challenge="a" * 64),
        require_idle=False,
    )
    prepared = _prepared_idle_eviction_subject(
        source,
        token_expires_at=source.db_now + timedelta(seconds=1),
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                requester.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(victim,),
                leases=(_workload_lease(victim),),
            )
        ],
    )
    begin_kwargs: dict = {}
    prepared_expiries: list[datetime] = []

    async def read_busy(db, **kwargs):
        assert kwargs["backend_id"] == victim.backend_id
        assert kwargs["gpu_resource_id"] == requester.gpu_resource_id
        return source

    async def prepare_busy(session_factory, expected_subject, *, token_expires_at):
        assert expected_subject is source
        prepared_expiries.append(token_expires_at)
        return replace(prepared, token_expires_at=token_expires_at)

    async def capacity_available(resource_id: str, **kwargs):
        begin_kwargs.update(kwargs)
        return GPUIdleEvictionResult(
            status="capacity_available",
            reason="capacity_available",
            committed_mb=0,
            shortfall_mb=0,
        )

    monkeypatch.setattr(
        authority_module,
        "read_gpu_busy_eviction_runtime_subject",
        read_busy,
    )
    monkeypatch.setattr(
        authority_module,
        "prepare_gpu_idle_eviction_runtime_generation",
        prepare_busy,
    )
    monkeypatch.setattr(store, "begin_idle_eviction", capacity_available)

    outcome = await authority_module._evict_one_idle_victim(
        _session_factory(events),
        store,  # type: ignore[arg-type]
        _RecordingSigner(events),  # type: ignore[arg-type]
        requester,
        victim,
        health_refresher=lambda backend_id, challenge: asyncio.sleep(0, result=True),
        hard_ttl_ms=1_000,
        heartbeat_interval_seconds=5,
        queue_deadline=time.monotonic() + 120,
        ticket_expires_at_ms=int(time.time() * 1000) + 120_000,
        allow_busy=True,
    )

    assert outcome == "capacity_available"
    assert begin_kwargs["hard_ttl_ms"] == 31_000
    assert begin_kwargs["allow_busy"] is True
    assert prepared_expiries == [source.db_now + timedelta(seconds=1)]


@pytest.mark.asyncio
async def test_busy_eviction_end_to_end_waits_then_unloads(monkeypatch) -> None:
    events: list[str] = []
    requester = replace(_cold_subject(), budget_mb=4096)
    victim = _allocation(uuid.UUID(int=54), budget_mb=4096, generation="7")
    source = replace(
        _idle_eviction_subject(victim, requester.gpu_resource_id, challenge="a" * 64),
        require_idle=False,
    )
    prepared = _prepared_idle_eviction_subject(
        source,
        token_expires_at=source.db_now + timedelta(seconds=60),
    )
    draining = replace(
        victim,
        state=GPUAllocationState.DRAINING,
        generation=prepared.generation,
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                requester.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(draining,),
                transition_present=True,
            )
        ],
    )

    async def read_busy(db, **kwargs):
        events.append("read_busy")
        assert kwargs["backend_id"] == victim.backend_id
        return source

    async def prepare_busy(session_factory, expected_subject, *, token_expires_at):
        events.append("prepare_busy")
        assert expected_subject is source
        return replace(prepared, token_expires_at=token_expires_at)

    async def read_ready_health(
        session_factory,
        expected_subject,
        *,
        challenge: str,
    ) -> GPUEvictionDrainHealth:
        events.append("read_drain_health")
        assert expected_subject.generation == prepared.generation
        return GPUEvictionDrainHealth(
            status="ready_to_unload",
            reason="ready_to_unload",
        )

    async def commit_phase(
        session_factory,
        passed_store,
        expected_subject,
        *,
        phase: str,
        challenge: str | None,
        owner_id: str,
    ) -> GPUEvictionCommitResult:
        events.append(f"commit_{phase}")
        assert passed_store is store
        assert expected_subject.generation == prepared.generation
        assert challenge is not None
        return GPUEvictionCommitResult(
            status="finalized",
            state=(
                GPUAllocationState.UNLOADING
                if phase == "drain"
                else GPUAllocationState.UNLOADED
            ),
            reason=phase,
        )

    class BusyEvictionClient:
        def __init__(
            self, backend: MLBackendRegistry, *, shadow_session_factory=None
        ) -> None:
            assert backend.id == prepared.backend_registry_id

        async def lifecycle_drain(self, grant):
            events.append("drain")
            grant.report_response(200)
            return _busy_drain_response(prepared, active_requests=1)

        async def lifecycle_unload(self, grant):
            events.append("unload")
            grant.report_response(200)
            return _unload_response(prepared)

    async def refresh_health(backend_id: uuid.UUID, challenge: str) -> bool:
        events.append("health")
        assert backend_id == prepared.backend_registry_id
        return True

    monkeypatch.setattr(
        authority_module,
        "read_gpu_busy_eviction_runtime_subject",
        read_busy,
    )
    monkeypatch.setattr(
        authority_module,
        "prepare_gpu_idle_eviction_runtime_generation",
        prepare_busy,
    )
    monkeypatch.setattr(
        authority_module,
        "_read_busy_eviction_drain_health",
        read_ready_health,
    )
    monkeypatch.setattr(
        authority_module,
        "commit_gpu_eviction_phase_from_health",
        commit_phase,
    )
    monkeypatch.setattr(authority_module, "MLBackendClient", BusyEvictionClient)

    outcome = await authority_module._evict_one_idle_victim(
        _session_factory(events),
        store,  # type: ignore[arg-type]
        _ScopeSigner(events),  # type: ignore[arg-type]
        requester,
        victim,
        health_refresher=refresh_health,
        hard_ttl_ms=120_000,
        heartbeat_interval_seconds=5,
        queue_deadline=time.monotonic() + 120,
        ticket_expires_at_ms=int(time.time() * 1000) + 120_000,
        allow_busy=True,
        poll_interval_seconds=0.001,
    )

    assert outcome == "unloaded"
    assert store.begin_kwargs[0]["allow_busy"] is True
    assert events.index("drain") < events.index("read_drain_health")
    assert events.index("read_drain_health") < events.index("commit_drain")
    assert events.index("commit_drain") < events.index("unload")
    assert events.index("unload") < events.index("commit_unload")
    assert store.eviction_release_kwargs[-1]["generation"] == prepared.generation


@pytest.mark.asyncio
async def test_busy_eviction_cancel_orders_durable_intent_arm_resume_and_proof(
    monkeypatch,
) -> None:
    events: list[str] = []
    cancel_subject = _prepared_eviction_cancel_subject()
    drain_subject = GPUPreparedIdleEvictionRuntimeSubject(
        backend=cancel_subject.backend,
        backend_registry_id=cancel_subject.backend_registry_id,
        gpu_resource_id=cancel_subject.gpu_resource_id,
        membership_epoch=cancel_subject.membership_epoch,
        budget_mb=cancel_subject.budget_mb,
        eviction_priority=cancel_subject.eviction_priority,
        max_concurrency=cancel_subject.max_concurrency,
        boot_id=cancel_subject.boot_id,
        source_generation=cancel_subject.source_generation,
        generation=cancel_subject.drain_generation,
        pool_ids=cancel_subject.pool_ids,
        control_epoch=cancel_subject.control_epoch,
        runtime_epoch=cancel_subject.runtime_epoch,
        token_expires_at=cancel_subject.drain_token_expires_at,
        require_idle=False,
        db_now=cancel_subject.db_now,
    )
    prepare_attempts = 0

    async def prepare_cancel(*args, **kwargs):
        nonlocal prepare_attempts
        prepare_attempts += 1
        events.append("prepare_cancel")
        assert args[1] is drain_subject
        assert kwargs["owner_id"] == cancel_subject.owner_id
        if prepare_attempts == 1:
            raise TimeoutError("lost durable cancel intent response")
        return cancel_subject

    class CancelStore:
        arm_attempts = 0

        async def arm_eviction_cancel(self, resource_id: str, **kwargs):
            self.arm_attempts += 1
            events.append("arm_cancel")
            assert resource_id == cancel_subject.gpu_resource_id
            assert kwargs["expected_generation"] == cancel_subject.drain_generation
            if self.arm_attempts == 1:
                raise TimeoutError("lost arm response")
            return GPUEvictionBranchResult(
                status="armed",
                branch="cancel",
                state=GPUAllocationState.DRAINING,
                generation=cancel_subject.drain_generation,
                idempotent=True,
            )

    class CancelClient:
        def __init__(
            self, backend: MLBackendRegistry, *, shadow_session_factory=None
        ) -> None:
            assert backend.id == cancel_subject.backend_registry_id

        async def lifecycle_cancel_drain(self, grant):
            events.append("resume")
            grant.report_response(200)
            assert grant.generation == cancel_subject.generation
            return _cancel_response(cancel_subject)

    async def refresh_health(backend_id: uuid.UUID, challenge: str) -> bool:
        events.append("health")
        assert backend_id == cancel_subject.backend_registry_id
        return True

    async def commit_cancel(*args, **kwargs):
        events.append("commit_cancel")
        assert args[2] is cancel_subject
        assert kwargs["ack_confirmed"] is True
        assert kwargs["challenge"] is not None
        return GPUEvictionCommitResult(
            status="finalized",
            state=GPUAllocationState.RESIDENT,
            reason="cancelled_resident",
        )

    monkeypatch.setattr(
        authority_module,
        "prepare_gpu_eviction_cancel_runtime_generation",
        prepare_cancel,
    )
    monkeypatch.setattr(authority_module, "MLBackendClient", CancelClient)
    monkeypatch.setattr(
        authority_module,
        "commit_gpu_eviction_cancel_from_health",
        commit_cancel,
    )
    signer = _ScopeSigner(events)
    result, branch = await authority_module._cancel_busy_eviction(
        _session_factory(events),
        CancelStore(),  # type: ignore[arg-type]
        signer,  # type: ignore[arg-type]
        drain_subject,
        health_refresher=refresh_health,
        owner_id=cancel_subject.owner_id,
        owner_hard_deadline_ms=cancel_subject.owner_hard_deadline_ms,
    )

    assert result is not None and result.state is GPUAllocationState.RESIDENT
    assert branch == "cancel"
    assert events == [
        "prepare_cancel",
        "prepare_cancel",
        "sign",
        "arm_cancel",
        "arm_cancel",
        "resume",
        "health",
        "commit_cancel",
    ]


@pytest.mark.asyncio
async def test_busy_eviction_cancel_never_sends_resume_after_unload_branch_wins(
    monkeypatch,
) -> None:
    events: list[str] = []
    cancel_subject = _prepared_eviction_cancel_subject()
    drain_subject = GPUPreparedIdleEvictionRuntimeSubject(
        backend=cancel_subject.backend,
        backend_registry_id=cancel_subject.backend_registry_id,
        gpu_resource_id=cancel_subject.gpu_resource_id,
        membership_epoch=cancel_subject.membership_epoch,
        budget_mb=cancel_subject.budget_mb,
        eviction_priority=cancel_subject.eviction_priority,
        max_concurrency=cancel_subject.max_concurrency,
        boot_id=cancel_subject.boot_id,
        source_generation=cancel_subject.source_generation,
        generation=cancel_subject.drain_generation,
        pool_ids=cancel_subject.pool_ids,
        control_epoch=cancel_subject.control_epoch,
        runtime_epoch=cancel_subject.runtime_epoch,
        token_expires_at=cancel_subject.drain_token_expires_at,
        require_idle=False,
        db_now=cancel_subject.db_now,
    )

    async def prepare_cancel(*args, **kwargs):
        events.append("prepare_cancel")
        return cancel_subject

    class ConflictStore:
        async def arm_eviction_cancel(self, resource_id: str, **kwargs):
            events.append("arm_cancel")
            return GPUEvictionBranchResult(
                status="branch_conflict",
                branch="unload",
                state=GPUAllocationState.UNLOADING,
                generation=cancel_subject.drain_generation,
            )

    class ForbiddenClient:
        def __init__(
            self, backend: MLBackendRegistry, *, shadow_session_factory=None
        ) -> None:
            raise AssertionError("unload branch must fence real RESUME")

    async def no_health(backend_id: uuid.UUID, challenge: str) -> bool:
        raise AssertionError("unload branch must skip cancel health")

    monkeypatch.setattr(
        authority_module,
        "prepare_gpu_eviction_cancel_runtime_generation",
        prepare_cancel,
    )
    monkeypatch.setattr(authority_module, "MLBackendClient", ForbiddenClient)
    signer = _ScopeSigner(events)
    result, branch = await authority_module._cancel_busy_eviction(
        _session_factory(events),
        ConflictStore(),  # type: ignore[arg-type]
        signer,  # type: ignore[arg-type]
        drain_subject,
        health_refresher=no_health,
        owner_id=cancel_subject.owner_id,
        owner_hard_deadline_ms=cancel_subject.owner_hard_deadline_ms,
    )

    assert result is None
    assert branch == "unload"
    assert events == ["prepare_cancel", "sign", "arm_cancel"]


def test_resident_authority_builder_is_lazy() -> None:
    events: list[str] = []

    def sessions():
        events.append("db")
        raise AssertionError("session factory must stay lazy")

    def open_store():
        events.append("store")
        raise AssertionError("store factory must stay lazy")

    def load_signer():
        events.append("signer")
        raise AssertionError("signer factory must stay lazy")

    authority_module.build_gpu_dispatch_context_factory(
        sessions,  # type: ignore[arg-type]
        store_factory=open_store,  # type: ignore[arg-type]
        signer_factory=load_signer,  # type: ignore[arg-type]
    )

    assert events == []


@pytest.mark.parametrize(
    "kwargs",
    (
        {"admission_timeout_seconds": 0},
        {"admission_timeout_seconds": float("nan")},
        {"admission_timeout_seconds": 3601},
        {"residency_cooldown_seconds": 0},
        {"residency_cooldown_seconds": float("nan")},
        {"residency_cooldown_seconds": 3601},
        {"queue_poll_interval_seconds": 0},
        {"queue_poll_interval_seconds": float("inf")},
    ),
)
def test_resident_authority_rejects_invalid_fifo_timings(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        authority_module.build_gpu_dispatch_context_factory(
            _session_factory([]),
            **kwargs,
        )


@pytest.mark.parametrize(
    ("card_queue", "error_code"),
    (
        (False, GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED),
        (True, GPUArbiterErrorCode.CAPACITY_UNAVAILABLE),
    ),
)
def test_fifo_queue_full_maps_to_retryable_dispatch_error(
    card_queue: bool,
    error_code: GPUArbiterErrorCode,
) -> None:
    error = authority_module._map_queue_rejection(
        GPUQueueResult(status="full", ticket_id="ticket:test"),
        card_queue=card_queue,
    )

    assert error.error_code == error_code.value
    assert error.headers == {"Retry-After": "1"}


def test_expired_card_ticket_rejection_remains_retryable() -> None:
    error = authority_module._map_admission_rejection(
        GPUAdmissionResult(
            status="card_queued",
            reason="card_fifo_wait",
            committed_mb=4096,
            lease_count=0,
        )
    )

    assert error.error_code == GPUArbiterErrorCode.CAPACITY_UNAVAILABLE.value
    assert error.headers == {"Retry-After": "1"}


@pytest.mark.asyncio
async def test_resident_authority_orders_horizon_signing_and_release(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _subject()
    store = _RecordingStore(events)
    signer = _RecordingSigner(events)
    _install_authority_fakes(monkeypatch, events, subject)

    def load_signer():
        events.append("signer")
        return signer

    def open_store():
        events.append("store")
        return store

    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=open_store,  # type: ignore[arg-type]
        signer_factory=load_signer,  # type: ignore[arg-type]
    )
    async with factory(_request(subject)) as grant:
        events.append("yield")
        assert grant.report_response(503) is True

    assert events == [
        "db",
        "subject",
        "signer",
        "store",
        "admit",
        "horizon",
        "sign",
        "heartbeat",
        "yield",
        "release",
        "close",
    ]
    assert store.admit_kwargs is not None
    assert store.admit_kwargs["require_resident"] is True
    assert store.admit_kwargs["evictable"] is False
    assert store.admit_kwargs["generation"] == subject.generation
    assert store.admit_kwargs["membership_epoch"] == subject.membership_epoch
    lease_uuid = uuid.UUID(store.admit_kwargs["lease_id"].removeprefix("workload:"))
    owner_uuid = uuid.UUID(store.admit_kwargs["owner_id"].removeprefix("dispatch:"))
    assert owner_uuid != lease_uuid
    assert store.heartbeat_kwargs[0]["owner_id"] == store.admit_kwargs["owner_id"]
    assert store.release_kwargs[0]["owner_id"] == store.admit_kwargs["owner_id"]
    assert signer.claims is not None
    assert signer.claims.jti == store.admit_kwargs["lease_id"]
    assert signer.claims.owner is None
    assert signer.claims.operation is None
    assert store.hard_deadline_ms is not None
    assert signer.claims.exp == store.hard_deadline_ms // 1000


@pytest.mark.asyncio
async def test_resident_authority_marks_missing_outcome_uncertain(monkeypatch) -> None:
    events: list[str] = []
    subject = _subject()
    store = _RecordingStore(events)
    signer = _RecordingSigner(events)
    _install_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
    )

    async with factory(_request(subject)):
        pass

    assert events[-2:] == ["uncertain", "close"]
    assert "release" not in events
    assert store.admit_kwargs is not None
    assert store.uncertain_kwargs[0]["owner_id"] == store.admit_kwargs["owner_id"]


@pytest.mark.asyncio
async def test_resident_authority_releases_only_its_proven_unsent_lease(monkeypatch):
    events = []
    subject = _subject()
    store = _RecordingStore(events)
    _install_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,
        signer_factory=lambda: _RecordingSigner(events),
    )
    async with factory(_request(subject)) as grant:
        assert grant.report_not_sent()
    assert events[-2:] == ["release", "close"]
    assert store.uncertain_kwargs == []
    assert store.release_kwargs[0]["lease_id"] == store.admit_kwargs["lease_id"]
    assert store.release_kwargs[0]["owner_id"] == store.admit_kwargs["owner_id"]
    assert store.release_kwargs[0]["generation"] == subject.generation


@pytest.mark.asyncio
async def test_resident_authority_releases_when_horizon_revalidation_fails(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _subject()
    store = _RecordingStore(events)
    signer = _RecordingSigner(events)
    _install_authority_fakes(monkeypatch, events, subject)

    async def reject_horizon(*args, **kwargs):
        events.append("horizon")
        raise GPUResidentRuntimeSubjectError("runtime_subject_changed")

    monkeypatch.setattr(
        authority_module,
        "record_gpu_resident_runtime_token_expiry",
        reject_horizon,
    )
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
    )

    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.NOT_READY.value
    assert "sign" not in events
    assert events[-2:] == ["release", "close"]


@pytest.mark.asyncio
async def test_resident_authority_cleans_up_same_lease_after_admit_error(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _subject()
    store = _RecordingStore(events, admission_status="error")
    signer = _RecordingSigner(events)
    _install_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
    )

    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.UNAVAILABLE.value
    assert store.admit_kwargs is not None
    assert store.release_kwargs[0]["lease_id"] == store.admit_kwargs["lease_id"]
    assert store.release_kwargs[0]["owner_id"] == store.admit_kwargs["owner_id"]
    assert store.admit_attempts == 2
    assert events[-4:] == ["admit", "admit", "release", "close"]


@pytest.mark.asyncio
async def test_resident_authority_releases_when_pre_yield_heartbeat_fails(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _subject()
    store = _RecordingStore(events, heartbeat_status="stale")
    signer = _RecordingSigner(events)
    _install_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
    )

    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.UNAVAILABLE.value
    assert events[-3:] == ["heartbeat", "release", "close"]


@pytest.mark.asyncio
async def test_resident_authority_releases_when_signing_fails(monkeypatch) -> None:
    events: list[str] = []
    subject = _subject()
    store = _RecordingStore(events)
    signer = _RecordingSigner(events, fail=True)
    _install_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
    )

    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.UNAVAILABLE.value
    assert events[-3:] == ["sign", "release", "close"]


@pytest.mark.asyncio
async def test_resident_authority_heartbeats_until_response(monkeypatch) -> None:
    events: list[str] = []
    subject = _subject()
    store = _RecordingStore(events)
    signer = _RecordingSigner(events)
    _install_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        heartbeat_ttl_ms=1_000,
        heartbeat_interval_seconds=0.001,
    )

    async with factory(_request(subject)) as grant:
        for _ in range(100):
            if events.count("heartbeat") >= 3:
                break
            await asyncio.sleep(0.001)
        assert events.count("heartbeat") >= 3
        grant.report_response(200)

    assert events[-2:] == ["release", "close"]


@pytest.mark.asyncio
async def test_resident_authority_maps_concurrency_and_rejects_unload_before_io(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _subject()
    store = _RecordingStore(events, admission_status="concurrency_saturated")
    signer = _RecordingSigner(events)
    _install_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        admission_timeout_seconds=0.001,
        queue_poll_interval_seconds=0.001,
    )

    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")
    assert caught.value.error_code == (
        GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED.value
    )
    assert caught.value.headers == {"Retry-After": "1"}
    assert events[-1] == "close"

    events.clear()
    with pytest.raises(GPUArbiterDispatchError) as unload:
        async with factory(
            _request(
                subject,
                operation="unload",
                scope=AdmissionScope.UNLOAD,
            )
        ):
            raise AssertionError("unload must not enter workload authority")
    assert unload.value.error_code == GPUArbiterErrorCode.NOT_READY.value
    assert events == []


@pytest.mark.asyncio
async def test_resident_authority_waits_in_backend_fifo_and_consumes_exact_ticket(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _subject()
    store = _RecordingStore(
        events,
        admission_statuses=[
            "concurrency_saturated",
            "concurrency_saturated",
            "admitted",
        ],
    )
    signer = _RecordingSigner(events)
    _install_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        admission_timeout_seconds=1,
        queue_poll_interval_seconds=0.001,
    )

    async with factory(_request(subject)) as grant:
        grant.report_response(200)

    assert store.admit_attempts == 3
    assert len(store.backend_enqueue_kwargs) == 1
    queued = store.backend_enqueue_kwargs[0]
    ticket_id = queued["ticket_id"]
    assert queued["backend_id"] == str(subject.backend_registry_id)
    assert queued["membership_epoch"] == subject.membership_epoch
    assert queued["owner_id"] == store.admit_kwargs_history[0]["owner_id"]
    assert 0 < queued["ttl_ms"] <= 1_000
    assert "backend_ticket_id" not in store.admit_kwargs_history[0]
    assert [item["backend_ticket_id"] for item in store.admit_kwargs_history[1:]] == [
        ticket_id,
        ticket_id,
    ]
    assert store.queue_cancel_kwargs == []
    assert events.index("backend_enqueue") < events.index("horizon")


@pytest.mark.asyncio
async def test_fifo_enqueue_retry_keeps_original_deadline(monkeypatch) -> None:
    events: list[str] = []
    store = _RecordingStore(events)
    attempts: list[dict] = []

    async def lose_first_enqueue_response(resource_id: str, **kwargs):
        attempts.append({"resource_id": resource_id, **kwargs})
        if len(attempts) == 1:
            await asyncio.sleep(0.02)
            raise TimeoutError("enqueue response lost")
        return GPUQueueResult(
            status="queued",
            ticket_id=kwargs["ticket_id"],
            position=1,
            expires_at_ms=int(time.time() * 1000) + kwargs["ttl_ms"],
        )

    monkeypatch.setattr(store, "enqueue_backend", lose_first_enqueue_response)
    ticket = await authority_module._enqueue_fifo_ticket(
        store,  # type: ignore[arg-type]
        _RESOURCE_ID,
        backend_id="backend:test",
        membership_epoch=3,
        ticket_id="backend:ticket",
        owner_id="dispatch:owner",
        deadline=time.monotonic() + 0.2,
        card_queue=False,
    )

    assert ticket.status == "queued"
    assert len(attempts) == 2
    assert attempts[0]["ticket_id"] == attempts[1]["ticket_id"]
    assert attempts[0]["owner_id"] == attempts[1]["owner_id"]
    assert 0 < attempts[1]["ttl_ms"] < attempts[0]["ttl_ms"] <= 200


@pytest.mark.asyncio
async def test_resident_uncertain_admit_then_rejection_keeps_exact_cleanup(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _subject()
    store = _RecordingStore(
        events,
        admission_statuses=["error", "concurrency_saturated"],
    )
    signer = _RecordingSigner(events)
    _install_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
    )

    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.UNAVAILABLE.value
    assert store.admit_attempts == 2
    assert store.backend_enqueue_kwargs == []
    assert len(store.release_kwargs) == 1
    assert (
        store.release_kwargs[0]["lease_id"]
        == (store.admit_kwargs_history[0]["lease_id"])
    )
    assert (
        store.release_kwargs[0]["owner_id"]
        == (store.admit_kwargs_history[0]["owner_id"])
    )
    assert events[-2:] == ["release", "close"]


@pytest.mark.asyncio
async def test_resident_backend_fifo_timeout_cancels_ticket_before_store_close(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _subject()
    store = _RecordingStore(events, admission_status="concurrency_saturated")
    signer = _RecordingSigner(events)
    _install_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        admission_timeout_seconds=0.001,
        queue_poll_interval_seconds=0.001,
    )

    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == (
        GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED.value
    )
    assert caught.value.headers == {"Retry-After": "1"}
    assert len(store.backend_enqueue_kwargs) == 1
    assert len(store.queue_cancel_kwargs) == 1
    assert store.queue_cancel_kwargs[0]["card_queue"] is False
    assert (
        store.queue_cancel_kwargs[0]["ticket_id"]
        == (store.backend_enqueue_kwargs[0]["ticket_id"])
    )
    assert events[-2:] == ["backend_cancel", "close"]


@pytest.mark.asyncio
async def test_resident_authority_finishes_uncertain_cleanup_under_repeated_cancel(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _subject()
    store = _RecordingStore(events)
    store.block_uncertain = True
    signer = _RecordingSigner(events)
    _install_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
    )
    entered = asyncio.Event()

    async def dispatch() -> None:
        async with factory(_request(subject)):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(dispatch())
    await entered.wait()
    task.cancel()
    await store.uncertain_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False
    store.uncertain_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert events[-2:] == ["uncertain", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", (200, 503))
async def test_cold_authority_orders_generation_reservation_and_loading(
    monkeypatch,
    status_code: int,
) -> None:
    events: list[str] = []
    subject = _cold_subject()
    store = _RecordingStore(events)
    signer = _RecordingSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)

    def load_signer():
        events.append("signer")
        return signer

    def open_store():
        events.append("store")
        return store

    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=open_store,  # type: ignore[arg-type]
        signer_factory=load_signer,  # type: ignore[arg-type]
    )
    async with factory(_request(subject)) as grant:
        events.append("yield")
        assert grant.generation == "8"
        assert grant.report_response(status_code) is True

    ordered = [
        "signer",
        "store",
        "ping",
        "card_enqueue",
        "cold_owner",
        "prepare",
        "cold_revalidate",
        "admit",
        "sign",
        "heartbeat",
        "transition:loading",
        "yield",
        "health",
        "terminal:resident",
        "release",
        "close",
    ]
    assert [event for event in events if event in ordered] == ordered
    assert store.admit_kwargs is not None
    assert store.admit_kwargs["require_cold_owner"] is True
    assert store.admit_kwargs["evictable"] is False
    assert store.admit_kwargs["generation"] == "8"
    assert (
        store.admit_kwargs["card_ticket_id"]
        == (store.card_enqueue_kwargs[0]["ticket_id"])
    )
    assert store.card_enqueue_kwargs[0]["owner_id"] == (store.admit_kwargs["owner_id"])
    assert store.cold_owner_kwargs[0]["owner_id"] == store.admit_kwargs["owner_id"]
    assert store.cold_revalidate_kwargs[0]["owner_id"] == store.admit_kwargs["owner_id"]
    assert signer.claims is not None
    assert signer.claims.generation == "8"
    assert signer.claims.jti == store.admit_kwargs["lease_id"]
    assert store.hard_deadline_ms is not None
    assert signer.claims.exp == store.hard_deadline_ms // 1000
    assert len(store.release_kwargs) == 1
    assert store.uncertain_kwargs == []


@pytest.mark.asyncio
async def test_cold_authority_waits_for_card_fifo_head_before_capacity_or_owner(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _cold_subject()
    store = _RecordingStore(events, card_queue_positions=[2, 2, 1])
    signer = _RecordingSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        admission_timeout_seconds=1,
        queue_poll_interval_seconds=0.001,
    )

    async with factory(_request(subject)) as grant:
        grant.report_response(200)

    assert events.index("card_position:1") < events.index("cold_owner")
    assert events.count("card_position:2") == 2
    assert len(store.card_enqueue_kwargs) == 1
    assert len(store.queue_position_kwargs) == 3
    assert all(item["card_queue"] is True for item in store.queue_position_kwargs)
    assert (store.admit_kwargs or {})["card_ticket_id"] == (
        store.card_enqueue_kwargs[0]["ticket_id"]
    )
    assert store.queue_cancel_kwargs == []


@pytest.mark.asyncio
async def test_cold_card_fifo_timeout_cancels_ticket_without_capacity_work(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _cold_subject()
    store = _RecordingStore(events, card_queue_positions=[2])
    signer = _RecordingSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        admission_timeout_seconds=0.001,
        queue_poll_interval_seconds=0.001,
    )

    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.CAPACITY_UNAVAILABLE.value
    assert caught.value.headers == {"Retry-After": "1"}
    assert "cold_owner" not in events
    assert "prepare" not in events
    assert "admit" not in events
    assert len(store.queue_cancel_kwargs) == 1
    assert store.queue_cancel_kwargs[0]["card_queue"] is True
    assert events[-2:] == ["card_cancel", "close"]


@pytest.mark.asyncio
async def test_cold_card_fifo_cancellation_cleans_ticket_before_store_close(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _cold_subject()
    store = _RecordingStore(events, card_queue_positions=[2])
    signer = _RecordingSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        admission_timeout_seconds=1,
        queue_poll_interval_seconds=1,
    )

    async def dispatch() -> None:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    task = asyncio.create_task(dispatch())
    for _ in range(100):
        if "card_enqueue" in events:
            break
        await asyncio.sleep(0)
    assert "card_enqueue" in events
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "cold_owner" not in events
    assert len(store.queue_cancel_kwargs) == 1
    assert (
        store.queue_cancel_kwargs[0]["ticket_id"]
        == (store.card_enqueue_kwargs[0]["ticket_id"])
    )
    assert events[-2:] == ["card_cancel", "close"]


@pytest.mark.asyncio
async def test_cold_cooldown_timeout_waits_then_cleans_exact_ticket(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _cold_subject()
    victim = _allocation(
        uuid.UUID(int=41),
        budget_mb=4096,
        generation="3",
        eviction_priority=0,
        not_evict_before_ms=1_100,
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(victim,),
                observed_at_ms=100,
            )
        ],
    )
    signer = _RecordingSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        admission_timeout_seconds=0.2,
        queue_poll_interval_seconds=0.001,
    )

    async def dispatch() -> None:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    task = asyncio.create_task(dispatch())
    for _ in range(100):
        if f"snapshot:{subject.gpu_resource_id}" in events:
            break
        await asyncio.sleep(0)
    assert f"snapshot:{subject.gpu_resource_id}" in events
    await asyncio.sleep(0)
    assert task.done() is False

    with pytest.raises(GPUArbiterDispatchError) as caught:
        await task

    assert caught.value.error_code == GPUArbiterErrorCode.CAPACITY_UNAVAILABLE.value
    assert caught.value.headers == {"Retry-After": "1"}
    assert "cold_owner" not in events
    assert "prepare" not in events
    assert "admit" not in events
    assert store.begin_kwargs == []
    assert len(store.card_enqueue_kwargs) == 1
    assert len(store.queue_cancel_kwargs) == 1
    assert (
        store.queue_cancel_kwargs[0]["ticket_id"]
        == store.card_enqueue_kwargs[0]["ticket_id"]
    )
    assert events[-2:] == ["card_cancel", "close"]


@pytest.mark.asyncio
async def test_cold_cooldown_cancellation_cleans_ticket_before_store_close(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _cold_subject()
    victim = _allocation(
        uuid.UUID(int=42),
        budget_mb=4096,
        generation="3",
        eviction_priority=0,
        not_evict_before_ms=1_100,
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(victim,),
                observed_at_ms=100,
            )
        ],
    )
    signer = _RecordingSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        admission_timeout_seconds=1,
        queue_poll_interval_seconds=0.001,
    )

    async def dispatch() -> None:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    task = asyncio.create_task(dispatch())
    for _ in range(100):
        if f"snapshot:{subject.gpu_resource_id}" in events:
            break
        await asyncio.sleep(0)
    assert f"snapshot:{subject.gpu_resource_id}" in events
    await asyncio.sleep(0)
    assert task.done() is False
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "cold_owner" not in events
    assert "prepare" not in events
    assert "admit" not in events
    assert store.begin_kwargs == []
    assert len(store.card_enqueue_kwargs) == 1
    assert len(store.queue_cancel_kwargs) == 1
    assert (
        store.queue_cancel_kwargs[0]["ticket_id"]
        == store.card_enqueue_kwargs[0]["ticket_id"]
    )
    assert events[-2:] == ["card_cancel", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ("finalized", "stale"))
async def test_cold_authority_keeps_unknown_or_stale_terminal_lease_uncertain(
    monkeypatch,
    terminal_status: str,
) -> None:
    events: list[str] = []
    subject = _cold_subject()
    store = _RecordingStore(events)
    signer = _RecordingSigner(events)
    _install_cold_authority_fakes(
        monkeypatch,
        events,
        subject,
        terminal_state=GPUAllocationState.UNKNOWN,
        terminal_status=terminal_status,
    )
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
    )

    async with factory(_request(subject)) as grant:
        grant.report_response(200)

    assert events[-4:] == [
        "health",
        "terminal:unknown",
        "uncertain",
        "close",
    ]
    assert store.release_kwargs == []
    assert len(store.uncertain_kwargs) == 1


@pytest.mark.asyncio
async def test_cold_authority_heartbeats_through_terminal_commit(monkeypatch) -> None:
    events: list[str] = []
    subject = _cold_subject()
    store = _RecordingStore(events)
    signer = _RecordingSigner(events)
    prepared = _install_cold_authority_fakes(monkeypatch, events, subject)

    async def commit_after_heartbeat(
        session_factory,
        passed_store,
        expected_subject,
        *,
        challenge: str | None,
        lease_id: str,
        owner_id: str,
        resident_cooldown_ms: int,
    ) -> GPUColdTerminalCommitResult:
        assert passed_store is store
        assert expected_subject.generation == prepared.generation
        assert challenge == "a" * 64
        assert resident_cooldown_ms == 30_000
        heartbeat_count = events.count("heartbeat")
        for _ in range(100):
            if events.count("heartbeat") > heartbeat_count:
                break
            await asyncio.sleep(0.001)
        assert events.count("heartbeat") > heartbeat_count
        events.append("terminal:resident")
        return GPUColdTerminalCommitResult(
            status="finalized",
            state=GPUAllocationState.RESIDENT,
            reason="resident",
        )

    monkeypatch.setattr(
        authority_module,
        "commit_gpu_cold_terminal_from_health",
        commit_after_heartbeat,
    )
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        heartbeat_ttl_ms=1_000,
        heartbeat_interval_seconds=0.001,
    )

    async with factory(_request(subject)) as grant:
        grant.report_response(200)

    assert events[-2:] == ["release", "close"]


@pytest.mark.asyncio
async def test_cold_authority_releases_intent_when_revalidation_is_missing(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _cold_subject()
    store = _RecordingStore(events, cold_revalidate_status="missing")
    signer = _RecordingSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
    )

    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.NOT_READY.value
    assert "prepare" in events
    assert "admit" not in events
    assert "sign" not in events
    assert events[-3:] == ["cold_release", "card_cancel", "close"]
    assert store.cold_release_kwargs[0]["generation"] == "8"


@pytest.mark.asyncio
async def test_cold_authority_rolls_back_unexposed_signing_failure(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _cold_subject()
    store = _RecordingStore(events)
    signer = _RecordingSigner(events, fail=True)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
    )

    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.UNAVAILABLE.value
    assert events[-3:] == ["finalize:unloaded", "release", "close"]
    assert store.uncertain_kwargs == []
    assert (
        store.finalize_kwargs[-1]["request_owner_id"]
        == (store.admit_kwargs or {})["owner_id"]
    )


@pytest.mark.asyncio
async def test_cold_authority_retracts_only_its_proven_unsent_loading_reservation(
    monkeypatch,
):
    events = []
    subject = _cold_subject()
    store = _RecordingStore(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,
        signer_factory=lambda: _RecordingSigner(events),
    )
    async with factory(_request(subject)) as grant:
        assert grant.report_not_sent()
    assert events[-3:] == ["finalize:unloaded", "release", "close"]
    assert store.uncertain_kwargs == []
    assert not any(event.startswith("terminal:") for event in events)
    final = store.finalize_kwargs[-1]
    assert final["request_owner_id"] == store.admit_kwargs["owner_id"]
    assert final["request_lease_id"] == store.admit_kwargs["lease_id"]
    assert final["expected_generation"] == store.admit_kwargs["generation"]


@pytest.mark.asyncio
async def test_cold_authority_rolls_back_invalid_unexposed_grant(monkeypatch) -> None:
    events: list[str] = []
    subject = _cold_subject()
    store = _RecordingStore(events)
    signer = _RecordingSigner(events, token=" ")
    _install_cold_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
    )

    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.UNAVAILABLE.value
    assert events[-3:] == ["finalize:unloaded", "release", "close"]
    assert store.uncertain_kwargs == []


@pytest.mark.asyncio
async def test_cold_authority_retries_exact_admission_after_response_loss(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _cold_subject()
    store = _RecordingStore(events, admission_status="error_once")
    signer = _RecordingSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
    )

    async with factory(_request(subject)) as grant:
        grant.report_uncertain_if_missing("request_aborted")

    assert store.admit_attempts == 2
    assert events.count("admit") == 2
    assert "transition:loading" in events
    assert "health" not in events
    assert events[-3:] == ["terminal:unknown", "uncertain", "close"]


@pytest.mark.asyncio
async def test_cold_authority_rolls_back_when_exact_admission_retry_is_lost(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _cold_subject()
    store = _RecordingStore(events, admission_status="error")
    signer = _RecordingSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
    )

    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.UNAVAILABLE.value
    assert store.admit_attempts == 2
    assert events[-5:] == [
        "finalize:unloaded",
        "release",
        "cold_release",
        "card_cancel",
        "close",
    ]
    assert store.uncertain_kwargs == []


@pytest.mark.asyncio
async def test_cold_uncertain_admit_then_rejection_keeps_exact_cleanup(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _cold_subject()
    store = _RecordingStore(
        events,
        admission_statuses=["error", "card_queued"],
    )
    signer = _RecordingSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
    )

    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.UNAVAILABLE.value
    assert store.admit_attempts == 2
    assert len(store.finalize_kwargs) == 1
    assert store.finalize_kwargs[0]["target_state"] is GPUAllocationState.UNLOADED
    assert len(store.release_kwargs) == 1
    assert (
        store.release_kwargs[0]["lease_id"]
        == (store.admit_kwargs_history[0]["lease_id"])
    )
    assert (
        store.release_kwargs[0]["owner_id"]
        == (store.admit_kwargs_history[0]["owner_id"])
    )
    assert events[-5:] == [
        "finalize:unloaded",
        "release",
        "cold_release",
        "card_cancel",
        "close",
    ]


@pytest.mark.asyncio
async def test_cold_authority_uses_durable_horizon_when_it_is_earlier(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _cold_subject()
    store = _RecordingStore(events)
    signer = _RecordingSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    config = Settings(_env_file=None, ml_predict_timeout=1)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        config=config,
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
    )

    async with factory(_request(subject)) as grant:
        grant.report_response(200)

    assert signer.claims is not None
    assert signer.claims.exp == int(
        (subject.db_now + timedelta(seconds=31)).timestamp()
    )


@pytest.mark.asyncio
async def test_cold_authority_rejects_invalid_ttl_before_secret_redis_or_prepare(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _cold_subject()
    _install_cold_authority_fakes(monkeypatch, events, subject)

    def load_signer():
        events.append("signer")
        raise AssertionError("signer must not be loaded")

    def open_store():
        events.append("store")
        raise AssertionError("store must not be opened")

    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        config=Settings(_env_file=None, ml_predict_timeout=3_000_000),
        store_factory=open_store,  # type: ignore[arg-type]
        signer_factory=load_signer,  # type: ignore[arg-type]
    )
    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.CONFIG_INVALID.value
    assert "signer" not in events
    assert "store" not in events
    assert "prepare" not in events


@pytest.mark.asyncio
async def test_cold_authority_requires_post_token_reconcile_window(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _cold_subject()
    _install_cold_authority_fakes(monkeypatch, events, subject)

    def load_signer():
        events.append("signer")
        raise AssertionError("signer must not be loaded")

    def open_store():
        events.append("store")
        raise AssertionError("store must not be opened")

    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        config=Settings(_env_file=None, ml_predict_timeout=151),
        store_factory=open_store,  # type: ignore[arg-type]
        signer_factory=load_signer,  # type: ignore[arg-type]
    )
    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.CONFIG_INVALID.value
    assert "reconcile proof window" in str(caught.value)
    assert "signer" not in events
    assert "store" not in events
    assert "prepare" not in events


@pytest.mark.asyncio
async def test_cold_authority_finishes_uncertain_cleanup_under_repeated_cancel(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = _cold_subject()
    store = _RecordingStore(events)
    store.block_uncertain = True
    signer = _RecordingSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
    )
    entered = asyncio.Event()

    async def dispatch() -> None:
        async with factory(_request(subject)):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(dispatch())
    await entered.wait()
    task.cancel()
    await store.uncertain_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False
    store.uncertain_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "health" not in events
    assert events[-3:] == ["terminal:unknown", "uncertain", "close"]


@pytest.mark.asyncio
async def test_cold_authority_rejects_exhausted_generation_before_secret_or_redis(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = replace(
        _cold_subject(),
        generation_high_water=9_223_372_036_854_775_807,
    )
    _install_cold_authority_fakes(monkeypatch, events, subject)

    def load_signer():
        events.append("signer")
        raise AssertionError("signer must not be loaded")

    def open_store():
        events.append("store")
        raise AssertionError("store must not be opened")

    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=open_store,  # type: ignore[arg-type]
        signer_factory=load_signer,  # type: ignore[arg-type]
    )
    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.NOT_READY.value
    assert "signer" not in events
    assert "store" not in events


def test_idle_victim_hint_uses_priority_lru_and_protects_higher_priority() -> None:
    requester = replace(
        _cold_subject(),
        budget_mb=4096,
        eviction_priority=2,
    )
    oldest_same_priority = _allocation(
        uuid.UUID(int=2),
        budget_mb=2048,
        generation="3",
        eviction_priority=1,
        last_used_at_ms=10,
    )
    newer_same_priority = _allocation(
        uuid.UUID(int=1),
        budget_mb=2048,
        generation="3",
        eviction_priority=1,
        last_used_at_ms=20,
    )
    lower_priority = _allocation(
        uuid.UUID(int=3),
        budget_mb=2048,
        generation="3",
        eviction_priority=0,
        last_used_at_ms=30,
    )
    snapshot = _card_snapshot(
        requester.gpu_resource_id,
        allocatable_mb=6144,
        allocations=(
            oldest_same_priority,
            newer_same_priority,
            lower_priority,
        ),
    )

    assert authority_module._idle_victim_hint(snapshot, requester) is lower_priority

    tied_snapshot = _card_snapshot(
        requester.gpu_resource_id,
        allocatable_mb=4096,
        allocations=(newer_same_priority, oldest_same_priority),
    )
    assert (
        authority_module._idle_victim_hint(tied_snapshot, requester)
        is oldest_same_priority
    )

    protected = _allocation(
        uuid.UUID(int=4),
        budget_mb=4096,
        generation="3",
        eviction_priority=3,
    )
    with pytest.raises(GPUArbiterDispatchError) as caught:
        authority_module._idle_victim_hint(
            _card_snapshot(
                requester.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(protected,),
            ),
            requester,
        )
    assert caught.value.error_code == GPUArbiterErrorCode.CAPACITY_UNAVAILABLE.value
    assert caught.value.headers == {"Retry-After": "1"}


def test_idle_victim_hint_uses_snapshot_redis_time_for_cumulative_cooldown() -> None:
    requester = replace(
        _cold_subject(),
        budget_mb=4096,
        eviction_priority=1,
    )
    first = _allocation(
        uuid.UUID(int=21),
        budget_mb=2048,
        generation="3",
        eviction_priority=0,
        last_used_at_ms=10,
        not_evict_before_ms=120,
    )
    second = _allocation(
        uuid.UUID(int=22),
        budget_mb=2048,
        generation="3",
        eviction_priority=0,
        last_used_at_ms=20,
        not_evict_before_ms=140,
    )
    snapshot = _card_snapshot(
        requester.gpu_resource_id,
        allocatable_mb=4096,
        allocations=(first, second),
        observed_at_ms=100,
    )

    with pytest.raises(authority_module._GPUVictimCooldownActive) as caught:
        authority_module._idle_victim_hint(snapshot, requester)

    assert caught.value.retry_at_ms == 140
    expired = replace(snapshot, observed_at_ms=140)
    assert authority_module._idle_victim_hint(expired, requester) is first


@pytest.mark.asyncio
async def test_cold_capacity_waits_for_cumulative_cooldown_with_exact_ticket(
    monkeypatch,
) -> None:
    events: list[str] = []
    resource_id = "node-cooldown/index:0"
    requester = replace(
        _cold_subject(),
        gpu_resource_id=resource_id,
        budget_mb=4096,
        eviction_priority=1,
    )
    first = _allocation(
        uuid.UUID(int=31),
        budget_mb=2048,
        generation="3",
        eviction_priority=0,
        last_used_at_ms=10,
        not_evict_before_ms=120,
    )
    second = _allocation(
        uuid.UUID(int=32),
        budget_mb=2048,
        generation="5",
        eviction_priority=0,
        last_used_at_ms=20,
        not_evict_before_ms=140,
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                resource_id,
                allocatable_mb=4096,
                allocations=(first, second),
                observed_at_ms=100,
            ),
            _card_snapshot(
                resource_id,
                allocatable_mb=4096,
                allocations=(first, second),
                observed_at_ms=140,
            ),
            _card_snapshot(
                resource_id,
                allocatable_mb=4096,
                allocations=(second,),
                observed_at_ms=140,
            ),
            _card_snapshot(
                resource_id,
                allocatable_mb=4096,
                allocations=(),
                observed_at_ms=140,
            ),
        ],
    )
    waits: list[float] = []
    evictions: list[dict] = []

    async def record_sleep(seconds: float) -> None:
        waits.append(seconds)

    async def evict_one(*args, **kwargs) -> str:
        evictions.append(
            {
                "victim": args[4],
                "card_ticket_id": kwargs["requester_card_ticket_id"],
                "queue_owner_id": kwargs["requester_queue_owner_id"],
            }
        )
        return "unloaded"

    async def no_health(backend_id: uuid.UUID, challenge: str) -> bool:
        raise AssertionError("cooldown capacity test must not refresh health")

    monkeypatch.setattr(authority_module.asyncio, "sleep", record_sleep)
    monkeypatch.setattr(authority_module, "_evict_one_idle_victim", evict_one)
    changed = await authority_module._ensure_cold_capacity(
        _session_factory(events),
        store,  # type: ignore[arg-type]
        _RecordingSigner(events),  # type: ignore[arg-type]
        requester,
        health_refresher=no_health,
        hard_ttl_ms=120_000,
        heartbeat_interval_seconds=5,
        queue_deadline=time.monotonic() + 10,
        ticket_expires_at_ms=int(time.time() * 1000) + 10_000,
        requester_card_ticket_id="card-ticket",
        requester_queue_owner_id="queue-owner",
    )

    assert changed is True
    assert waits == pytest.approx([0.04])
    assert [item["victim"] for item in evictions] == [first, second]
    assert all(
        item["card_ticket_id"] == "card-ticket"
        and item["queue_owner_id"] == "queue-owner"
        for item in evictions
    )


@pytest.mark.asyncio
async def test_cold_capacity_uses_busy_victim_only_when_idle_capacity_is_insufficient(
    monkeypatch,
) -> None:
    events: list[str] = []
    requester = replace(_cold_subject(), budget_mb=4096, eviction_priority=1)
    victim = _allocation(
        uuid.UUID(int=41),
        budget_mb=4096,
        generation="3",
        eviction_priority=0,
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                requester.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(victim,),
                leases=(_workload_lease(victim),),
            ),
            _card_snapshot(
                requester.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(),
            ),
        ],
    )
    calls: list[dict] = []

    async def evict_one(*args, **kwargs) -> str:
        calls.append(kwargs)
        return "unloaded"

    async def no_health(backend_id: uuid.UUID, challenge: str) -> bool:
        raise AssertionError("busy selection test delegates health to eviction")

    monkeypatch.setattr(authority_module, "_evict_one_idle_victim", evict_one)
    changed = await authority_module._ensure_cold_capacity(
        _session_factory(events),
        store,  # type: ignore[arg-type]
        _RecordingSigner(events),  # type: ignore[arg-type]
        requester,
        health_refresher=no_health,
        hard_ttl_ms=120_000,
        heartbeat_interval_seconds=5,
        queue_deadline=time.monotonic() + 10,
        ticket_expires_at_ms=int(time.time() * 1000) + 10_000,
        requester_card_ticket_id="card-ticket",
        requester_queue_owner_id="queue-owner",
    )

    assert changed is True
    assert len(calls) == 1
    assert calls[0]["allow_busy"] is True


@pytest.mark.asyncio
async def test_cold_capacity_does_not_treat_not_ready_as_busy_fallback(
    monkeypatch,
) -> None:
    events: list[str] = []
    requester = replace(_cold_subject(), budget_mb=4096, eviction_priority=1)
    victim = _allocation(
        uuid.UUID(int=43),
        budget_mb=4096,
        generation="3",
        eviction_priority=0,
    )
    store = _EvictionStore(
        events,
        [
            replace(
                _card_snapshot(
                    requester.gpu_resource_id,
                    allocatable_mb=4096,
                    allocations=(victim,),
                    leases=(_workload_lease(victim),),
                ),
                ready=False,
            )
        ],
    )

    async def forbidden_eviction(*args, **kwargs) -> str:
        raise AssertionError("not-ready cards must not enter busy victim selection")

    monkeypatch.setattr(
        authority_module,
        "_evict_one_idle_victim",
        forbidden_eviction,
    )
    with pytest.raises(GPUArbiterDispatchError) as caught:
        await authority_module._ensure_cold_capacity(
            _session_factory(events),
            store,  # type: ignore[arg-type]
            _RecordingSigner(events),  # type: ignore[arg-type]
            requester,
            health_refresher=lambda backend_id, challenge: asyncio.sleep(
                0,
                result=True,
            ),
            hard_ttl_ms=120_000,
            heartbeat_interval_seconds=5,
            queue_deadline=time.monotonic() + 10,
            ticket_expires_at_ms=int(time.time() * 1000) + 10_000,
            requester_card_ticket_id="card-ticket",
            requester_queue_owner_id="queue-owner",
        )

    assert caught.value.error_code == GPUArbiterErrorCode.NOT_READY.value


@pytest.mark.asyncio
async def test_cold_cooldown_wait_isolated_per_gpu_resource() -> None:
    events: list[str] = []
    requester_a = replace(
        _cold_subject(),
        gpu_resource_id="node-cooldown/index:0",
    )
    requester_b = replace(
        _cold_subject(),
        backend_registry_id=uuid.uuid4(),
        gpu_resource_id="node-cooldown/index:1",
    )
    victim = _allocation(
        uuid.UUID(int=33),
        budget_mb=4096,
        generation="3",
        eviction_priority=0,
        not_evict_before_ms=1_100,
    )
    waiting_store = _EvictionStore(
        events,
        [
            _card_snapshot(
                requester_a.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(victim,),
                observed_at_ms=100,
            )
        ],
    )
    free_store = _RecordingStore(events)
    signer = _RecordingSigner(events)
    session_factory = _session_factory(events)
    queue_deadline = time.monotonic() + 1
    ticket_expires_at_ms = int(time.time() * 1000) + 1_000

    async def no_health(backend_id: uuid.UUID, challenge: str) -> bool:
        raise AssertionError("isolated cooldown wait must not refresh health")

    waiting = asyncio.create_task(
        authority_module._ensure_cold_capacity(
            session_factory,  # type: ignore[arg-type]
            waiting_store,  # type: ignore[arg-type]
            signer,  # type: ignore[arg-type]
            requester_a,
            health_refresher=no_health,
            hard_ttl_ms=120_000,
            heartbeat_interval_seconds=5,
            queue_deadline=queue_deadline,
            ticket_expires_at_ms=ticket_expires_at_ms,
            requester_card_ticket_id="card-a",
            requester_queue_owner_id="owner-a",
        )
    )
    for _ in range(100):
        if f"snapshot:{requester_a.gpu_resource_id}" in events:
            break
        await asyncio.sleep(0)
    assert f"snapshot:{requester_a.gpu_resource_id}" in events

    free = await asyncio.wait_for(
        authority_module._ensure_cold_capacity(
            session_factory,  # type: ignore[arg-type]
            free_store,  # type: ignore[arg-type]
            signer,  # type: ignore[arg-type]
            requester_b,
            health_refresher=no_health,
            hard_ttl_ms=120_000,
            heartbeat_interval_seconds=5,
            queue_deadline=queue_deadline,
            ticket_expires_at_ms=ticket_expires_at_ms,
            requester_card_ticket_id="card-b",
            requester_queue_owner_id="owner-b",
        ),
        timeout=0.2,
    )
    assert free is False
    assert waiting.done() is False
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting


def test_idle_victim_hint_does_not_evict_for_an_allocated_requester() -> None:
    requester = _cold_subject()
    requester_allocation = replace(
        _allocation(
            requester.backend_registry_id,
            budget_mb=requester.budget_mb,
            generation="8",
        ),
        state=GPUAllocationState.RESERVING,
        evictable=False,
        reservation_lease_id="workload:existing",
        reservation_owner_id="dispatch:existing",
    )
    victim = _allocation(
        uuid.UUID(int=6),
        budget_mb=4096,
        generation="3",
    )

    with pytest.raises(GPUArbiterDispatchError) as caught:
        authority_module._idle_victim_hint(
            _card_snapshot(
                requester.gpu_resource_id,
                allocatable_mb=8192,
                allocations=(requester_allocation, victim),
            ),
            requester,
        )

    assert caught.value.error_code == GPUArbiterErrorCode.NOT_READY.value


@pytest.mark.asyncio
async def test_eviction_heartbeat_fails_at_hard_deadline() -> None:
    events: list[str] = []
    victim = _allocation(
        uuid.UUID(int=5),
        budget_mb=2048,
        generation="3",
    )
    idle = _idle_eviction_subject(
        victim,
        _RESOURCE_ID,
        challenge="a" * 64,
    )
    prepared = _prepared_idle_eviction_subject(
        idle,
        token_expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    store = _RecordingStore(events)

    with pytest.raises(GPUArbiterStoreError, match="hard deadline"):
        await authority_module._heartbeat_eviction_owner(
            store,  # type: ignore[arg-type]
            prepared,
            owner_id="evict:test",
            heartbeat_ttl_ms=30_000,
            heartbeat_interval_seconds=1,
            hard_deadline_ms=int(time.time() * 1000) - 1,
        )

    await authority_module._heartbeat_runtime_lease(
        store,  # type: ignore[arg-type]
        _subject(),
        lease_id="workload:test",
        owner_id="dispatch:test",
        heartbeat_ttl_ms=15_000,
        heartbeat_interval_seconds=1,
        hard_deadline_ms=int(time.time() * 1000) - 1,
    )
    assert events == []


@pytest.mark.asyncio
async def test_cold_capacity_deadline_stops_before_next_victim(monkeypatch) -> None:
    events: list[str] = []
    subject = replace(_cold_subject(), budget_mb=4096)
    victim = _allocation(
        uuid.UUID(int=10),
        budget_mb=2048,
        generation="6",
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(victim,),
            )
        ],
    )
    signer = _RecordingSigner(events)
    eviction_attempts = 0

    async def finish_first_victim_after_deadline(*args, **kwargs) -> str:
        nonlocal eviction_attempts
        eviction_attempts += 1
        await asyncio.sleep(0.02)
        return "unloaded"

    monkeypatch.setattr(
        authority_module,
        "_evict_one_idle_victim",
        finish_first_victim_after_deadline,
    )

    with pytest.raises(GPUArbiterDispatchError) as caught:
        await authority_module._ensure_cold_capacity(
            _session_factory(events),
            store,  # type: ignore[arg-type]
            signer,  # type: ignore[arg-type]
            subject,
            health_refresher=lambda backend_id, challenge: asyncio.sleep(
                0,
                result=True,
            ),
            hard_ttl_ms=120_000,
            heartbeat_interval_seconds=5,
            queue_deadline=time.monotonic()
            + authority_module._GPU_RUNTIME_EVICTION_CLEANUP_RESERVE_SECONDS
            + 0.01,
            ticket_expires_at_ms=int(time.time() * 1000)
            + int(authority_module._GPU_RUNTIME_EVICTION_CLEANUP_RESERVE_SECONDS * 1000)
            + 1_000,
            requester_card_ticket_id="card:ticket",
            requester_queue_owner_id="dispatch:owner",
        )

    assert caught.value.error_code == GPUArbiterErrorCode.CAPACITY_UNAVAILABLE.value
    assert caught.value.headers == {"Retry-After": "1"}
    assert eviction_attempts == 1
    assert events.count(f"snapshot:{subject.gpu_resource_id}") == 1


@pytest.mark.asyncio
async def test_cold_eviction_timeout_reserves_owner_for_unknown_cleanup(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = replace(_cold_subject(), budget_mb=4096)
    victim = _allocation(
        uuid.UUID(int=15),
        budget_mb=4096,
        generation="6",
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(victim,),
            )
        ],
    )
    signer = _ScopeSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    prepared_by_id = _install_eviction_authority_fakes(
        monkeypatch,
        events,
        (victim,),
        subject.gpu_resource_id,
    )
    owner_hard_deadline_ms: int | None = None
    original_begin = store.begin_idle_eviction

    async def record_owner_deadline(resource_id: str, **kwargs):
        nonlocal owner_hard_deadline_ms
        result = await original_begin(resource_id, **kwargs)
        owner_hard_deadline_ms = result.owner_hard_deadline_ms
        return result

    monkeypatch.setattr(store, "begin_idle_eviction", record_owner_deadline)

    class BlockingDrainClient:
        def __init__(
            self, backend: MLBackendRegistry, *, shadow_session_factory=None
        ) -> None:
            self.backend_id = backend.id

        async def lifecycle_drain(self, grant):
            events.append(f"drain:{self.backend_id}")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append(f"drain_cancelled:{self.backend_id}")
                raise

    monkeypatch.setattr(authority_module, "MLBackendClient", BlockingDrainClient)

    async def commit_unknown(
        session_factory,
        arbiter_store,
        expected_subject: GPUPreparedIdleEvictionRuntimeSubject,
        *,
        phase: str,
        challenge: str | None,
        owner_id: str,
    ) -> GPUEvictionCommitResult:
        events.append(f"commit_cleanup:{expected_subject.backend_registry_id}")
        assert expected_subject is prepared_by_id[expected_subject.backend_registry_id]
        assert arbiter_store is store
        assert phase == "drain"
        assert challenge is None
        assert owner_id.startswith("evict:")
        assert owner_hard_deadline_ms is not None
        assert int(time.time() * 1000) < owner_hard_deadline_ms
        return GPUEvictionCommitResult(
            status="finalized",
            state=GPUAllocationState.UNKNOWN,
            reason="eviction_response_uncertain",
        )

    monkeypatch.setattr(
        authority_module,
        "commit_gpu_eviction_phase_from_health",
        commit_unknown,
    )

    async def refresh_health(backend_id: uuid.UUID, challenge: str) -> bool:
        return True

    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        health_refresher=refresh_health,
        admission_timeout_seconds=(
            authority_module._GPU_RUNTIME_EVICTION_CLEANUP_RESERVE_SECONDS + 0.5
        ),
    )

    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.CAPACITY_UNAVAILABLE.value
    assert caught.value.headers == {"Retry-After": "1"}
    assert f"drain_cancelled:{victim.backend_id}" in events
    assert f"commit_cleanup:{victim.backend_id}" in events
    assert f"eviction_release:{victim.backend_id}" in events
    assert events.index(f"commit_cleanup:{victim.backend_id}") < events.index(
        f"eviction_release:{victim.backend_id}"
    )
    assert "cold_owner" not in events
    assert events[-2:] == ["card_cancel", "close"]


@pytest.mark.asyncio
async def test_cold_authority_evicts_multiple_idle_victims_before_target_owner(
    monkeypatch,
) -> None:
    events: list[str] = []
    resource_id = "node-runtime/index:1"
    subject = replace(
        _cold_subject(),
        gpu_resource_id=resource_id,
        budget_mb=4096,
        eviction_priority=2,
    )
    first_victim = _allocation(
        uuid.UUID(int=11),
        budget_mb=2048,
        generation="7",
        eviction_priority=0,
        last_used_at_ms=20,
    )
    second_victim = _allocation(
        uuid.UUID(int=12),
        budget_mb=2048,
        generation="9",
        eviction_priority=1,
        last_used_at_ms=10,
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                resource_id,
                allocatable_mb=4096,
                allocations=(second_victim, first_victim),
            ),
            _card_snapshot(
                resource_id,
                allocatable_mb=4096,
                allocations=(second_victim,),
            ),
            _card_snapshot(
                resource_id,
                allocatable_mb=4096,
                allocations=(),
            ),
        ],
    )
    signer = _ScopeSigner(events)
    refreshed_subject = replace(
        subject,
        observed_generation="8",
        generation_high_water=8,
        db_now=subject.db_now + timedelta(seconds=1),
    )
    cold_read_challenges: list[str | None] = []
    _install_cold_authority_fakes(
        monkeypatch,
        events,
        subject,
        cold_read_challenges=cold_read_challenges,
        refreshed_subject=refreshed_subject,
    )
    proof_challenges: dict[tuple[uuid.UUID, str], str | None] = {}
    _install_eviction_authority_fakes(
        monkeypatch,
        events,
        (first_victim, second_victim),
        resource_id,
        proof_challenges=proof_challenges,
    )
    health_challenges: dict[uuid.UUID, list[str]] = {}

    async def refresh_health(backend_id: uuid.UUID, challenge: str) -> bool:
        events.append(f"eviction_health:{backend_id}")
        assert len(challenge) == 64
        health_challenges.setdefault(backend_id, []).append(challenge)
        return True

    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        health_refresher=refresh_health,
    )
    async with factory(_request(subject)) as grant:
        grant.report_response(200)

    assert [item["victim_backend_id"] for item in store.begin_kwargs] == [
        first_victim.backend_id,
        second_victim.backend_id,
    ]
    assert all(item["resource_id"] == resource_id for item in store.begin_kwargs)
    card_ticket_id = store.card_enqueue_kwargs[0]["ticket_id"]
    queue_owner_id = store.card_enqueue_kwargs[0]["owner_id"]
    assert all(
        item["requester_card_ticket_id"] == card_ticket_id
        and item["requester_queue_owner_id"] == queue_owner_id
        for item in store.begin_kwargs
    )
    assert (store.admit_kwargs or {})["card_ticket_id"] == card_ticket_id
    assert all(
        item["resource_id"] == resource_id for item in store.eviction_release_kwargs
    )
    cold_owner_index = events.index("cold_owner")
    assert all(
        events.index(f"eviction_release:{victim.backend_id}") < cold_owner_index
        for victim in (first_victim, second_victim)
    )
    assert events.count("cold_subject") == 3
    assert events.count(f"eviction_health:{first_victim.backend_id}") == 3
    assert events.count(f"eviction_health:{second_victim.backend_id}") == 3
    assert events.count(f"eviction_health:{subject.backend_registry_id}") == 1
    assert cold_read_challenges == [
        None,
        None,
        health_challenges[subject.backend_registry_id][0],
    ]
    assert store.cold_owner_kwargs[0]["generation"] == "9"
    assert (store.admit_kwargs or {})["generation"] == "9"
    for victim in (first_victim, second_victim):
        backend_id = uuid.UUID(victim.backend_id)
        challenges = health_challenges[backend_id]
        assert len(challenges) == len(set(challenges)) == 3
        assert challenges == [
            proof_challenges[(backend_id, "initial")],
            proof_challenges[(backend_id, "drain")],
            proof_challenges[(backend_id, "unload")],
        ]

    eviction_claims = [
        claims for claims in signer.claims_history if claims.owner is not None
    ]
    assert len(eviction_claims) == 4
    by_backend: dict[str, list[AdmissionTokenClaims]] = {}
    for claims in eviction_claims:
        by_backend.setdefault(claims.backend_registry_id, []).append(claims)
        assert claims.operation == "evict"
        assert claims.gpu_resource_id == resource_id
    assert set(by_backend) == {first_victim.backend_id, second_victim.backend_id}
    for claims in by_backend.values():
        assert {item.scope for item in claims} == {
            AdmissionScope.DRAIN,
            AdmissionScope.UNLOAD,
        }
        assert len({item.jti for item in claims}) == 2
        assert len({item.owner for item in claims}) == 1
        assert len({item.generation for item in claims}) == 1


@pytest.mark.asyncio
async def test_cold_authority_rereads_target_after_eviction_before_issuing_owner(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = replace(_cold_subject(), budget_mb=4096)
    victim = _allocation(
        uuid.UUID(int=13),
        budget_mb=4096,
        generation="6",
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(victim,),
            ),
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(),
            ),
        ],
    )
    signer = _ScopeSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    _install_eviction_authority_fakes(
        monkeypatch,
        events,
        (victim,),
        subject.gpu_resource_id,
    )
    cold_reads = 0

    async def read_cold(
        db,
        *,
        backend_id: str,
        gpu_resource_id: str,
        expected_challenge: str | None = None,
    ):
        nonlocal cold_reads
        cold_reads += 1
        events.append("cold_subject")
        if cold_reads <= 2:
            assert expected_challenge is None
            return subject
        assert expected_challenge is not None
        raise authority_module.GPUColdRuntimeSubjectError("cold_runtime_not_ready")

    monkeypatch.setattr(authority_module, "read_gpu_cold_runtime_subject", read_cold)

    async def refresh_health(backend_id: uuid.UUID, challenge: str) -> bool:
        events.append(f"eviction_health:{backend_id}")
        return True

    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        health_refresher=refresh_health,
    )
    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.NOT_READY.value
    assert cold_reads == 3
    assert f"eviction_release:{victim.backend_id}" in events
    assert f"eviction_health:{subject.backend_registry_id}" in events
    assert "cold_owner" not in events
    assert "prepare" not in events


@pytest.mark.parametrize(
    ("health_failure", "expected_code"),
    (
        ("false", GPUArbiterErrorCode.NOT_READY),
        ("raise", GPUArbiterErrorCode.UNAVAILABLE),
    ),
)
@pytest.mark.asyncio
async def test_target_health_failure_after_eviction_blocks_cold_owner(
    monkeypatch,
    health_failure: str,
    expected_code: GPUArbiterErrorCode,
) -> None:
    events: list[str] = []
    subject = replace(_cold_subject(), budget_mb=4096)
    victim = _allocation(
        uuid.UUID(int=14),
        budget_mb=4096,
        generation="6",
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(victim,),
            ),
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(),
            ),
        ],
    )
    signer = _ScopeSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    _install_eviction_authority_fakes(
        monkeypatch,
        events,
        (victim,),
        subject.gpu_resource_id,
    )

    async def refresh_health(backend_id: uuid.UUID, challenge: str) -> bool:
        if backend_id == subject.backend_registry_id:
            if health_failure == "raise":
                raise TimeoutError("target health unavailable")
            return False
        return True

    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        health_refresher=refresh_health,
    )
    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == expected_code.value
    assert events.count("cold_subject") == 2
    assert f"eviction_release:{victim.backend_id}" in events
    assert "cold_owner" not in events
    assert "prepare" not in events


@pytest.mark.asyncio
async def test_cold_authority_rejects_when_only_higher_priority_victim_exists(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = replace(_cold_subject(), budget_mb=4096, eviction_priority=2)
    protected = _allocation(
        uuid.UUID(int=21),
        budget_mb=4096,
        generation="3",
        eviction_priority=3,
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(protected,),
            )
        ],
    )
    signer = _RecordingSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    health_calls: list[uuid.UUID] = []

    async def refresh_health(backend_id: uuid.UUID, challenge: str) -> bool:
        health_calls.append(backend_id)
        return True

    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        health_refresher=refresh_health,
    )
    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.CAPACITY_UNAVAILABLE.value
    assert caught.value.headers == {"Retry-After": "1"}
    assert health_calls == []
    assert store.begin_kwargs == []
    assert "cold_owner" not in events
    assert "prepare" not in events
    assert events[-1] == "close"


@pytest.mark.parametrize("all_unload_responses_lost", (False, True))
@pytest.mark.asyncio
async def test_eviction_retries_same_route_token_with_fresh_outcome_channel(
    monkeypatch,
    all_unload_responses_lost: bool,
) -> None:
    events: list[str] = []
    subject = replace(_cold_subject(), budget_mb=4096)
    victim = _allocation(
        uuid.UUID(int=31),
        budget_mb=4096,
        generation="4",
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(victim,),
            ),
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(),
            ),
        ],
    )
    signer = _ScopeSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    prepared_by_id = _install_eviction_authority_fakes(
        monkeypatch,
        events,
        (victim,),
        subject.gpu_resource_id,
    )
    attempts: dict[str, list] = {"drain": [], "unload": []}
    release_attempts = 0
    original_release = store.release_transition_owner

    async def lose_first_release_response(resource_id: str, **kwargs):
        nonlocal release_attempts
        release_attempts += 1
        if release_attempts == 1:
            await original_release(resource_id, **kwargs)
            raise TimeoutError("release response lost")
        return GPUTransitionOwnerResult(status="missing")

    monkeypatch.setattr(
        store,
        "release_transition_owner",
        lose_first_release_response,
    )

    class RetryClient:
        def __init__(
            self, backend: MLBackendRegistry, *, shadow_session_factory=None
        ) -> None:
            self.backend_id = backend.id

        async def lifecycle_drain(self, grant):
            attempts["drain"].append(grant)
            if len(attempts["drain"]) == 1:
                grant.report_uncertain_if_missing("request_aborted")
                raise RuntimeError("response lost")
            grant.report_response(200)
            return _drain_response(prepared_by_id[self.backend_id])

        async def lifecycle_unload(self, grant):
            attempts["unload"].append(grant)
            if all_unload_responses_lost or len(attempts["unload"]) == 1:
                grant.report_uncertain_if_missing("request_aborted")
                raise RuntimeError("response lost")
            grant.report_response(200)
            return _unload_response(prepared_by_id[self.backend_id])

    monkeypatch.setattr(authority_module, "MLBackendClient", RetryClient)

    async def refresh_health(backend_id: uuid.UUID, challenge: str) -> bool:
        return True

    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        health_refresher=refresh_health,
    )
    async with factory(_request(subject)) as grant:
        grant.report_response(200)

    for route_attempts in attempts.values():
        assert len(route_attempts) == 2
        assert route_attempts[0] is not route_attempts[1]
        assert route_attempts[0].admission_token == route_attempts[1].admission_token
    assert attempts["drain"][0].admission_token != attempts["unload"][0].admission_token
    assert release_attempts == 2


@pytest.mark.parametrize("lost_phase", ("drain", "unload"))
@pytest.mark.asyncio
async def test_eviction_replays_lost_terminal_commit_before_cleanup(
    monkeypatch,
    lost_phase: str,
) -> None:
    events: list[str] = []
    subject = replace(_cold_subject(), budget_mb=4096)
    victim = _allocation(
        uuid.UUID(int=32),
        budget_mb=4096,
        generation="4",
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(victim,),
            ),
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(),
            ),
        ],
    )
    signer = _ScopeSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    _install_eviction_authority_fakes(
        monkeypatch,
        events,
        (victim,),
        subject.gpu_resource_id,
    )
    commit_calls: list[tuple[str, str | None]] = []
    lost_challenge: str | None = None

    async def lose_terminal_response(
        session_factory,
        passed_store,
        expected_subject,
        *,
        phase: str,
        challenge: str | None,
        owner_id: str,
    ) -> GPUEvictionCommitResult:
        nonlocal lost_challenge
        commit_calls.append((phase, challenge))
        if phase == lost_phase and challenge is not None:
            if lost_challenge is None:
                lost_challenge = challenge
                raise TimeoutError("terminal CAS responses lost")
            assert challenge == lost_challenge
            state = (
                GPUAllocationState.UNLOADING
                if phase == "drain"
                else GPUAllocationState.UNLOADED
            )
            return GPUEvictionCommitResult(
                status="finalized",
                state=state,
                reason=state.value,
                idempotent=True,
            )
        if phase == "drain":
            return GPUEvictionCommitResult(
                status="finalized",
                state=GPUAllocationState.UNLOADING,
                reason="ready_to_unload",
            )
        assert challenge is None
        return GPUEvictionCommitResult(
            status="finalized",
            state=GPUAllocationState.UNKNOWN,
            reason="eviction_response_uncertain",
        )

    monkeypatch.setattr(
        authority_module,
        "commit_gpu_eviction_phase_from_health",
        lose_terminal_response,
    )

    async def refresh_health(backend_id: uuid.UUID, challenge: str) -> bool:
        return True

    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        health_refresher=refresh_health,
    )
    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.UNAVAILABLE.value
    if lost_phase == "drain":
        assert [phase for phase, _ in commit_calls] == [
            "drain",
            "drain",
            "unload",
        ]
        assert commit_calls[0][1] == commit_calls[1][1] == lost_challenge
        assert commit_calls[2][1] is None
    else:
        assert [phase for phase, _ in commit_calls] == [
            "drain",
            "unload",
            "unload",
        ]
        assert commit_calls[1][1] == commit_calls[2][1] == lost_challenge
    assert f"eviction_release:{victim.backend_id}" in events
    assert "cold_owner" not in events


@pytest.mark.asyncio
async def test_uncertain_begin_still_runs_conservative_owner_cleanup(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = replace(_cold_subject(), budget_mb=4096)
    victim = _allocation(
        uuid.UUID(int=33),
        budget_mb=4096,
        generation="4",
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(victim,),
            )
        ],
    )
    signer = _ScopeSigner(events)
    _install_eviction_authority_fakes(
        monkeypatch,
        events,
        (victim,),
        subject.gpu_resource_id,
    )
    begin_attempts = 0
    cleanup_calls = 0

    async def uncertain_begin(resource_id: str, **kwargs):
        nonlocal begin_attempts
        begin_attempts += 1
        if begin_attempts == 1:
            raise TimeoutError("begin response lost")
        return GPUIdleEvictionResult(
            status="capacity_available",
            reason="capacity_available",
            committed_mb=0,
            shortfall_mb=0,
        )

    async def reject_cleanup(
        session_factory,
        passed_store,
        expected_subject,
        *,
        phase: str,
        challenge: str | None,
        owner_id: str,
    ) -> GPUEvictionCommitResult:
        nonlocal cleanup_calls
        cleanup_calls += 1
        assert phase == "drain"
        assert challenge is None
        return GPUEvictionCommitResult(
            status="stale",
            state=GPUAllocationState.UNKNOWN,
            reason="redis_missing",
        )

    monkeypatch.setattr(store, "begin_idle_eviction", uncertain_begin)
    monkeypatch.setattr(
        authority_module,
        "commit_gpu_eviction_phase_from_health",
        reject_cleanup,
    )

    async def refresh_health(backend_id: uuid.UUID, challenge: str) -> bool:
        return True

    outcome = await authority_module._evict_one_idle_victim(
        _session_factory(events),
        store,  # type: ignore[arg-type]
        signer,  # type: ignore[arg-type]
        subject,
        victim,
        health_refresher=refresh_health,
        hard_ttl_ms=120_000,
        heartbeat_interval_seconds=5,
        queue_deadline=time.monotonic() + 120,
        ticket_expires_at_ms=int(time.time() * 1000) + 120_000,
    )

    assert outcome == "capacity_available"
    assert begin_attempts == 2
    assert cleanup_calls == 1
    assert store.eviction_release_kwargs == []


@pytest.mark.asyncio
async def test_uncertain_victim_stops_before_evicting_another_or_target(
    monkeypatch,
) -> None:
    events: list[str] = []
    subject = replace(_cold_subject(), budget_mb=4096)
    first_victim = _allocation(
        uuid.UUID(int=41),
        budget_mb=2048,
        generation="4",
        eviction_priority=0,
    )
    second_victim = _allocation(
        uuid.UUID(int=42),
        budget_mb=2048,
        generation="4",
        eviction_priority=1,
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(first_victim, second_victim),
            )
        ],
    )
    signer = _ScopeSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    _install_eviction_authority_fakes(
        monkeypatch,
        events,
        (first_victim, second_victim),
        subject.gpu_resource_id,
    )

    class InvalidDrainClient:
        def __init__(
            self, backend: MLBackendRegistry, *, shadow_session_factory=None
        ) -> None:
            self.backend_id = backend.id

        async def lifecycle_drain(self, grant):
            events.append(f"invalid_drain:{self.backend_id}")
            grant.report_response(503)
            return object()

        async def lifecycle_unload(self, grant):
            raise AssertionError("unload must not be attempted")

    async def commit_unknown(
        session_factory,
        passed_store,
        expected_subject,
        *,
        phase: str,
        challenge: str | None,
        owner_id: str,
    ) -> GPUEvictionCommitResult:
        events.append(f"commit_unknown:{expected_subject.backend_registry_id}")
        assert phase == "drain"
        assert challenge is None
        return GPUEvictionCommitResult(
            status="finalized",
            state=GPUAllocationState.UNKNOWN,
            reason="eviction_response_uncertain",
        )

    monkeypatch.setattr(authority_module, "MLBackendClient", InvalidDrainClient)
    monkeypatch.setattr(
        authority_module,
        "commit_gpu_eviction_phase_from_health",
        commit_unknown,
    )

    health_calls: list[uuid.UUID] = []

    async def refresh_health(backend_id: uuid.UUID, challenge: str) -> bool:
        health_calls.append(backend_id)
        return True

    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        health_refresher=refresh_health,
    )
    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.CAPACITY_UNAVAILABLE.value
    assert [item["victim_backend_id"] for item in store.begin_kwargs] == [
        first_victim.backend_id
    ]
    assert health_calls == [uuid.UUID(first_victim.backend_id)]
    assert "cold_owner" not in events
    assert f"idle_subject:{second_victim.backend_id}" not in events
    assert f"eviction_release:{first_victim.backend_id}" in events


@pytest.mark.parametrize(
    ("failed_phase", "health_failure", "lost_unload_responses"),
    (
        ("drain", "false", False),
        ("drain", "raise", False),
        ("unload", "false", False),
        ("unload", "raise", False),
        ("unload", "false", True),
        ("unload", "raise", True),
    ),
)
@pytest.mark.asyncio
async def test_phase_health_failure_converges_unknown_and_stops_eviction(
    monkeypatch,
    failed_phase: str,
    health_failure: str,
    lost_unload_responses: bool,
) -> None:
    events: list[str] = []
    subject = replace(_cold_subject(), budget_mb=4096)
    victim = _allocation(
        uuid.UUID(int=50),
        budget_mb=4096,
        generation="4",
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(victim,),
            ),
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(),
            ),
        ],
    )
    signer = _ScopeSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    _install_eviction_authority_fakes(
        monkeypatch,
        events,
        (victim,),
        subject.gpu_resource_id,
    )
    if lost_unload_responses:

        class LostUnloadResponseClient(authority_module.MLBackendClient):
            async def lifecycle_unload(self, grant):
                events.append(f"unload:{self.backend_id}")
                grant.report_uncertain_if_missing("request_aborted")
                raise TimeoutError("unload response lost")

        monkeypatch.setattr(
            authority_module, "MLBackendClient", LostUnloadResponseClient
        )
    commit_calls: list[tuple[str, str | None]] = []

    async def commit_phase(
        session_factory,
        passed_store,
        expected_subject,
        *,
        phase: str,
        challenge: str | None,
        owner_id: str,
    ) -> GPUEvictionCommitResult:
        commit_calls.append((phase, challenge))
        state = (
            GPUAllocationState.UNKNOWN
            if challenge is None
            else (
                GPUAllocationState.UNLOADING
                if phase == "drain"
                else GPUAllocationState.UNLOADED
            )
        )
        return GPUEvictionCommitResult(
            status="finalized",
            state=state,
            reason=state.value,
        )

    monkeypatch.setattr(
        authority_module,
        "commit_gpu_eviction_phase_from_health",
        commit_phase,
    )
    victim_health_calls = 0

    async def refresh_health(backend_id: uuid.UUID, challenge: str) -> bool:
        nonlocal victim_health_calls
        assert backend_id == uuid.UUID(victim.backend_id)
        victim_health_calls += 1
        phase = {1: "initial", 2: "drain", 3: "unload"}[victim_health_calls]
        if phase == failed_phase:
            if health_failure == "raise":
                raise TimeoutError("health unavailable")
            return False
        return True

    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        health_refresher=refresh_health,
    )
    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.CAPACITY_UNAVAILABLE.value
    assert commit_calls[-1] == (failed_phase, None)
    assert f"eviction_release:{victim.backend_id}" in events
    assert "cold_owner" not in events
    if failed_phase == "drain":
        assert not any(event.startswith("unload:") for event in events)
        assert victim_health_calls == 2
    else:
        assert any(event.startswith("unload:") for event in events)
        if lost_unload_responses:
            assert events.count(f"unload:{victim.backend_id}") == 2
        assert victim_health_calls == 3


@pytest.mark.parametrize("health_failure", ("false", "raise"))
@pytest.mark.asyncio
async def test_unavailable_initial_victim_health_sends_no_lifecycle_request(
    monkeypatch,
    health_failure: str,
) -> None:
    events: list[str] = []
    subject = replace(_cold_subject(), budget_mb=4096)
    victim = _allocation(
        uuid.UUID(int=51),
        budget_mb=4096,
        generation="4",
    )
    store = _EvictionStore(
        events,
        [
            _card_snapshot(
                subject.gpu_resource_id,
                allocatable_mb=4096,
                allocations=(victim,),
            )
        ],
    )
    signer = _ScopeSigner(events)
    _install_cold_authority_fakes(monkeypatch, events, subject)
    _install_eviction_authority_fakes(
        monkeypatch,
        events,
        (victim,),
        subject.gpu_resource_id,
    )

    async def refresh_health(backend_id: uuid.UUID, challenge: str) -> bool:
        assert backend_id == uuid.UUID(victim.backend_id)
        if health_failure == "raise":
            raise TimeoutError("health unavailable")
        return False

    factory = authority_module.build_gpu_dispatch_context_factory(
        _session_factory(events),
        store_factory=lambda: store,  # type: ignore[arg-type]
        signer_factory=lambda: signer,  # type: ignore[arg-type]
        health_refresher=refresh_health,
    )
    with pytest.raises(GPUArbiterDispatchError) as caught:
        async with factory(_request(subject)):
            raise AssertionError("grant must not be exposed")

    assert caught.value.error_code == GPUArbiterErrorCode.CAPACITY_UNAVAILABLE.value
    assert store.begin_kwargs == []
    assert not any(event.startswith("drain:") for event in events)
    assert not any(event.startswith("unload:") for event in events)
    assert "cold_owner" not in events
