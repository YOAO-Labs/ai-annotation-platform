"""Framework-neutral proof reset, resource repair, and runtime observation.

This owner evaluates typed Native proof facts and coordinates them with the
durable GPU ledger. Native calls borrow the caller-owned transaction; Redis
linearization remains in the existing ledger store.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import uuid
from typing import Any, Literal

import structlog

from aap_inference.gpu.settings import GPUAuthoritySettings as Settings
from aap_inference.gpu.native import GPUAuthorityDataProvider
from aap_inference.gpu.ledger import (
    GPUAllocation,
    GPUAllocationState,
    GPUArbiterStore,
    GPUArbiterStoreError,
    GPUBackendDomainMember,
    GPUCardSnapshot,
    GPUProofResetContext,
    GPUReconcileResult,
)
from aap_inference_contract.gpu import GPUAuthorityUnavailable
from aap_inference.gpu.policy import _HEALTH_EVIDENCE_MAX_AGE
from aap_inference.gpu.proofs import (
    GPUProofFence,
    GPUProofMembership,
    GPUProofRegistry,
    _GPUProofEvaluation,
    _GPUProofInvalid,
    _LockedGPUProofDomain,
    _PROOF_RESET_MAX_WINDOW,
    _datetime_to_epoch_ms,
    _gpu_domain_members,
    _health_managed_lifecycle_sha256,
    _optional_datetime_document,
    _parse_gpu_proof_probe,
    _parse_gpu_proof_residency,
    _registry_gpu_max_concurrency,
    _strict_nonnegative_int64,
    parse_gpu_proof_domain,
)
from aap_inference.gpu.resource import validate_gpu_resource_id


logger = structlog.get_logger(__name__)


def _unknown_proof_allocation(
    membership: GPUProofMembership,
    *,
    generation: str | None,
    last_used_at_ms: int,
) -> GPUAllocation:
    return GPUAllocation(
        backend_id=str(membership.backend_registry_id),
        state=GPUAllocationState.UNKNOWN,
        budget_mb=membership.vram_budget_mb,
        generation=generation,
        eviction_priority=membership.eviction_priority,
        evictable=False,
        max_concurrency=membership.max_concurrency,
        reservation_lease_id=None,
        reservation_owner_id=None,
        last_used_at_ms=last_used_at_ms,
        not_evict_before_ms=0,
    )


def _invalid_gpu_proof_evaluation(
    membership: GPUProofMembership,
    context: GPUProofResetContext,
    *,
    reason: str,
    probe_document: Any,
    residency_document: Any,
) -> _GPUProofEvaluation:
    return _GPUProofEvaluation(
        allocation=_unknown_proof_allocation(
            membership,
            generation=None,
            last_used_at_ms=context.prepared_at_ms,
        ),
        complete=False,
        reason=reason,
        evidence_deadline_ms=None,
        probe_document=probe_document,
        residency_document=residency_document,
    )


def _evaluate_gpu_member_proof(
    membership: GPUProofMembership,
    fence: GPUProofFence | None,
    registry: GPUProofRegistry | None,
    *,
    context: GPUProofResetContext,
    db_now: datetime,
    evidence_ttl: timedelta,
    residency_cooldown_ms: int,
) -> _GPUProofEvaluation:
    raw_health = registry.health_meta if registry is not None else None
    raw_probe = (
        raw_health.get("gpu_arbiter_probe") if type(raw_health) is dict else None
    )
    raw_residency = raw_health.get("residency") if type(raw_health) is dict else None

    if membership.state == "retiring":
        return _invalid_gpu_proof_evaluation(
            membership,
            context,
            reason="membership_retiring",
            probe_document=None,
            residency_document=None,
        )
    if membership.state == "pending":
        # Activation changes the Redis active domain.  A pending candidate must
        # first activate and obtain a new challenge bound to that exact state;
        # otherwise a post-commit activation could leave a ready pending-domain
        # snapshot behind.
        return _invalid_gpu_proof_evaluation(
            membership,
            context,
            reason="membership_pending",
            probe_document=raw_probe,
            residency_document=raw_residency,
        )
    if membership.state != "active":
        return _invalid_gpu_proof_evaluation(
            membership,
            context,
            reason="membership_state_invalid",
            probe_document=raw_probe,
            residency_document=raw_residency,
        )
    if fence is None:
        return _invalid_gpu_proof_evaluation(
            membership,
            context,
            reason="fence_missing",
            probe_document=raw_probe,
            residency_document=raw_residency,
        )
    if registry is None:
        return _invalid_gpu_proof_evaluation(
            membership,
            context,
            reason="registry_missing",
            probe_document=None,
            residency_document=None,
        )

    try:
        generation_high_water = _strict_nonnegative_int64(
            fence.generation_high_water, reason="generation_high_water_invalid"
        )
        control_epoch_high_water = _strict_nonnegative_int64(
            fence.control_epoch_high_water,
            reason="control_epoch_high_water_invalid",
        )
        runtime_epoch_high_water = _strict_nonnegative_int64(
            fence.runtime_epoch_high_water, reason="runtime_epoch_high_water_invalid"
        )
        runtime_epoch_baseline = _strict_nonnegative_int64(
            membership.runtime_epoch_baseline,
            reason="runtime_epoch_baseline_invalid",
        )
        if runtime_epoch_high_water <= runtime_epoch_baseline:
            raise _GPUProofInvalid("active_runtime_epoch_invalid")

        if (
            registry.state != "connected"
            or registry.gpu_resource_id != membership.gpu_resource_id
            or registry.vram_budget_mb != membership.vram_budget_mb
            or registry.eviction_priority != membership.eviction_priority
        ):
            raise _GPUProofInvalid("registry_claim_mismatch")
        registry_max_concurrency = _registry_gpu_max_concurrency(registry.extra_params)
        if registry_max_concurrency != membership.max_concurrency:
            raise _GPUProofInvalid("registry_concurrency_mismatch")

        probe = _parse_gpu_proof_probe(raw_probe)
        actual_capability_sha256 = _health_managed_lifecycle_sha256(raw_health)
        if probe.managed_lifecycle_sha256 is None or actual_capability_sha256 is None:
            raise _GPUProofInvalid("managed_lifecycle_capability_missing")
        if actual_capability_sha256 != probe.managed_lifecycle_sha256:
            raise _GPUProofInvalid("managed_lifecycle_capability_mismatch")
        if (
            probe.raw["backend_registry_id"] != str(membership.backend_registry_id)
            or probe.raw["gpu_resource_id"] != membership.gpu_resource_id
            or probe.raw["membership_epoch"] != str(membership.membership_epoch)
            or probe.raw["membership_state"] != membership.state
        ):
            raise _GPUProofInvalid("probe_membership_mismatch")
        if (
            registry.last_checked_at is None
            or registry.last_checked_at.tzinfo is None
            or registry.last_checked_at.utcoffset() is None
            or registry.last_checked_at.astimezone(UTC) != probe.observed_at
        ):
            raise _GPUProofInvalid("probe_registry_clock_mismatch")
        if probe.observed_at > db_now.astimezone(UTC):
            raise _GPUProofInvalid("probe_from_future")
        if db_now.astimezone(UTC) - probe.observed_at > evidence_ttl:
            raise _GPUProofInvalid("probe_expired")
        horizon = fence.token_expiry_high_water
        if horizon is None:
            raise _GPUProofInvalid("token_horizon_missing")
        if horizon.tzinfo is None or horizon.utcoffset() is None:
            raise _GPUProofInvalid("token_horizon_invalid")
        if probe.probe_started_at <= horizon.astimezone(UTC):
            raise _GPUProofInvalid("probe_not_after_token_horizon")

        residency = _parse_gpu_proof_residency(raw_residency)
        if residency.lifecycle_gate != "enforce":
            raise _GPUProofInvalid("lifecycle_gate_not_enforced")
        identity = residency.identity
        if identity is None:
            raise _GPUProofInvalid("residency_identity_missing")
        if (
            identity["backend_registry_id"] != str(membership.backend_registry_id)
            or identity["gpu_resource_id"] != membership.gpu_resource_id
        ):
            raise _GPUProofInvalid("residency_identity_mismatch")
        if residency.control_epoch is None:
            raise _GPUProofInvalid("residency_control_epoch_missing")
        if (
            residency.generation is not None
            and int(residency.generation) > generation_high_water
        ):
            raise _GPUProofInvalid("residency_generation_ahead")
        residency_control_epoch = int(residency.control_epoch)
        if residency_control_epoch > control_epoch_high_water:
            raise _GPUProofInvalid("residency_control_epoch_ahead")
        if residency_control_epoch < control_epoch_high_water:
            raise _GPUProofInvalid("residency_control_epoch_stale")

        observed_at_ms = _datetime_to_epoch_ms(probe.observed_at)
        evidence_deadline_ms = _datetime_to_epoch_ms(probe.observed_at + evidence_ttl)
        idle = (
            residency.active_requests == 0
            and residency.builders == 0
            and residency.borrowers == 0
            and not residency.draining
        )
        pools_complete = all(item is not None for item in residency.pool_residencies)
        if (
            idle
            and pools_complete
            and residency.gpu_loaded is False
            and residency.state in {"unloaded", "resident"}
            and not residency.evictable
            and all(item is False for item in residency.pool_residencies)
        ):
            return _GPUProofEvaluation(
                allocation=None,
                complete=True,
                reason="unloaded",
                evidence_deadline_ms=evidence_deadline_ms,
                probe_document=probe.raw,
                residency_document=residency.raw,
            )
        if (
            idle
            and pools_complete
            and residency.state == "resident"
            and residency.gpu_loaded is True
            and any(item is True for item in residency.pool_residencies)
            and residency.generation is not None
            and identity is not None
        ):
            return _GPUProofEvaluation(
                allocation=GPUAllocation(
                    backend_id=str(membership.backend_registry_id),
                    state=GPUAllocationState.RESIDENT,
                    budget_mb=membership.vram_budget_mb,
                    generation=residency.generation,
                    eviction_priority=membership.eviction_priority,
                    # The capability is challenge-bound, but declaration alone
                    # does not prove activation or signer-key acceptance.  Keep
                    # proof recovery non-evictable until signed promotion.
                    evictable=False,
                    max_concurrency=membership.max_concurrency,
                    reservation_lease_id=None,
                    reservation_owner_id=None,
                    last_used_at_ms=observed_at_ms,
                    not_evict_before_ms=(
                        context.prepared_at_ms + residency_cooldown_ms
                    ),
                ),
                complete=True,
                reason="resident",
                evidence_deadline_ms=evidence_deadline_ms,
                probe_document=probe.raw,
                residency_document=residency.raw,
            )
        return _GPUProofEvaluation(
            allocation=_unknown_proof_allocation(
                membership,
                generation=residency.generation,
                last_used_at_ms=observed_at_ms,
            ),
            complete=False,
            reason="residency_not_stably_idle",
            evidence_deadline_ms=evidence_deadline_ms,
            probe_document=probe.raw,
            residency_document=residency.raw,
        )
    except _GPUProofInvalid as exc:
        return _invalid_gpu_proof_evaluation(
            membership,
            context,
            reason=exc.reason,
            probe_document=raw_probe,
            residency_document=raw_residency,
        )


def _allocation_proof_document(allocation: GPUAllocation | None) -> Any:
    if allocation is None:
        return None
    return {
        "backend_id": allocation.backend_id,
        "state": allocation.state.value,
        "budget_mb": allocation.budget_mb,
        "generation": allocation.generation,
        "eviction_priority": allocation.eviction_priority,
        "evictable": allocation.evictable,
        "max_concurrency": allocation.max_concurrency,
        "last_used_at_ms": allocation.last_used_at_ms,
        "not_evict_before_ms": allocation.not_evict_before_ms,
    }


def _gpu_proof_fingerprint(
    context: GPUProofResetContext,
    locked: _LockedGPUProofDomain,
    evaluations: Mapping[uuid.UUID, _GPUProofEvaluation],
    *,
    evidence_deadline_ms: int,
    requested_ready: bool,
    config_matches: bool,
) -> str:
    members: list[dict[str, Any]] = []
    for membership in locked.memberships:
        backend_id = membership.backend_registry_id
        fence = locked.fences.get(backend_id)
        registry = locked.registries.get(backend_id)
        evaluation = evaluations[backend_id]
        members.append(
            {
                "membership": {
                    "backend_registry_id": str(backend_id),
                    "gpu_resource_id": membership.gpu_resource_id,
                    "membership_epoch": str(membership.membership_epoch),
                    "runtime_epoch_baseline": str(membership.runtime_epoch_baseline),
                    "state": membership.state,
                    "vram_budget_mb": membership.vram_budget_mb,
                    "eviction_priority": membership.eviction_priority,
                    "max_concurrency": membership.max_concurrency,
                    "retired_at": _optional_datetime_document(membership.retired_at),
                    "retire_reason": membership.retire_reason,
                    "retired_generation_high_water": (
                        membership.retired_generation_high_water
                    ),
                    "retired_control_epoch_high_water": (
                        membership.retired_control_epoch_high_water
                    ),
                    "retired_runtime_epoch_high_water": (
                        membership.retired_runtime_epoch_high_water
                    ),
                    "retired_token_expiry_high_water": (
                        _optional_datetime_document(
                            membership.retired_token_expiry_high_water
                        )
                    ),
                },
                "fence": (
                    None
                    if fence is None
                    else {
                        "generation_high_water": str(fence.generation_high_water),
                        "control_epoch_high_water": str(fence.control_epoch_high_water),
                        "runtime_epoch_high_water": str(fence.runtime_epoch_high_water),
                        "token_expiry_high_water": _optional_datetime_document(
                            fence.token_expiry_high_water
                        ),
                    }
                ),
                "registry": (
                    None
                    if registry is None
                    else {
                        "state": registry.state,
                        "gpu_resource_id": registry.gpu_resource_id,
                        "vram_budget_mb": registry.vram_budget_mb,
                        "eviction_priority": registry.eviction_priority,
                        "max_concurrency": (
                            registry.extra_params.get("max_concurrency", 4)
                            if type(registry.extra_params) is dict
                            else None
                        ),
                        "last_checked_at": _optional_datetime_document(
                            registry.last_checked_at
                        ),
                    }
                ),
                "probe": evaluation.probe_document,
                "residency": evaluation.residency_document,
                "verdict": {
                    "complete": evaluation.complete,
                    "reason": evaluation.reason,
                    "allocation": _allocation_proof_document(evaluation.allocation),
                },
            }
        )

    document = {
        "schema": "gpu-arbiter-proof/v1",
        "reset": {
            "reset_id": context.reset_id,
            "resource_id": context.resource_id,
            "allocatable_mb": context.allocatable_mb,
            "expected_reset_revision": context.ledger_revision,
            "expected_reset_incarnation": context.ledger_incarnation,
            "prepared_at_ms": context.prepared_at_ms,
            "begin_fingerprint": context.begin_fingerprint,
            "backend_memberships": [
                {
                    "backend_id": item.backend_id,
                    "membership_epoch": str(item.membership_epoch),
                    "state": item.state,
                }
                for item in context.backend_memberships
            ],
            "evidence_deadline_ms": evidence_deadline_ms,
        },
        "config_matches": config_matches,
        "requested_ready": requested_ready,
        "members": members,
    }
    canonical = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def commit_gpu_proof_reset_from_health(
    data_provider: GPUAuthorityDataProvider,
    store: GPUArbiterStore,
    context: GPUProofResetContext,
    *,
    config: Settings,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
    readiness_blocker: str | None = None,
) -> GPUReconcileResult:
    """Consume cached live-health proof under the durable resource lock.

    The Native action locks the PostgreSQL resource domain and returns credential-
    free proof facts. The borrowed transaction stays open through the Redis proof
    commit, preserving the database-lock/ledger-linearization order.
    """

    validate_gpu_resource_id(context.resource_id)
    if evidence_ttl <= timedelta(0) or evidence_ttl > _PROOF_RESET_MAX_WINDOW:
        raise ValueError(
            "evidence_ttl must be positive and no greater than five minutes"
        )
    if readiness_blocker is not None and (
        not isinstance(readiness_blocker, str)
        or not readiness_blocker
        or len(readiness_blocker) > 256
    ):
        raise ValueError("readiness_blocker must be a non-empty short string")

    async with data_provider.transaction() as data:
        reply = await data.step(
            "proof_reset_from_health",
            {
                "resource_id": context.resource_id,
                "expected_backend_memberships": [
                    {
                        "backend_id": item.backend_id,
                        "membership_epoch": str(item.membership_epoch),
                        "state": item.state,
                    }
                    for item in context.backend_memberships
                ],
                "wait_for_lock": True,
            },
        )
        if type(reply) is not dict or reply.get("status") not in {
            "matched",
            "domain_changed",
        }:
            raise GPUAuthorityUnavailable()
        locked = parse_gpu_proof_domain(reply)
        current_domain = _gpu_domain_members(locked.memberships)
        domain_matches = current_domain == context.backend_memberships
        if domain_matches != (reply["status"] == "matched"):
            raise GPUAuthorityUnavailable()
        configured_resource = config.gpu_arbiter_resources.get(context.resource_id)
        config_matches = (
            not config.gpu_arbiter_config_errors
            and configured_resource is not None
            and configured_resource.resource_id == context.resource_id
            and configured_resource.allocatable_mb == context.allocatable_mb
        )

        evaluations = {
            item.backend_registry_id: _evaluate_gpu_member_proof(
                item,
                locked.fences.get(item.backend_registry_id),
                locked.registries.get(item.backend_registry_id),
                context=context,
                db_now=locked.db_now,
                evidence_ttl=evidence_ttl,
                residency_cooldown_ms=(
                    config.gpu_arbiter_residency_cooldown_seconds * 1000
                ),
            )
            for item in locked.memberships
        }
        allocations = tuple(
            evaluation.allocation
            for evaluation in evaluations.values()
            if evaluation.allocation is not None
        )
        has_active_member = any(item.state == "active" for item in locked.memberships)
        requested_ready = (
            readiness_blocker is None
            and domain_matches
            and config_matches
            and has_active_member
            and len(locked.fences) == len(locked.memberships)
            and all(item.complete for item in evaluations.values())
            and sum(item.budget_mb for item in allocations) <= context.allocatable_mb
        )
        if not domain_matches:
            # The prepared marker owns its original closed domain. Clear that
            # snapshot conservatively; later legal domain evolution can reconcile.
            allocations = ()

        if requested_ready:
            proof_deadlines = [
                item.evidence_deadline_ms
                for item in evaluations.values()
                if item.evidence_deadline_ms is not None
            ]
            evidence_deadline_ms = min(proof_deadlines)
        else:
            # A not-ready rewrite grants no authority and remains restart-safe.
            evidence_deadline_ms = 0

        proof_fingerprint = _gpu_proof_fingerprint(
            context,
            locked,
            evaluations,
            evidence_deadline_ms=evidence_deadline_ms,
            requested_ready=requested_ready,
            config_matches=config_matches,
        )
        return await store.commit_proof_reset(
            context.resource_id,
            context.allocatable_mb,
            reset_id=context.reset_id,
            expected_reset_revision=context.ledger_revision,
            expected_reset_incarnation=context.ledger_incarnation,
            backend_memberships=context.backend_memberships,
            allocations=allocations,
            ready=requested_ready,
            evidence_deadline_ms=evidence_deadline_ms,
            proof_fingerprint=proof_fingerprint,
        )


@dataclass(frozen=True)
class GPUResourceRepairResult:
    resource_id: str
    action: Literal[
        "already_ready",
        "bootstrap",
        "repair",
        "resume_prepared",
    ]
    status: str
    ready: bool
    reason: str
    ledger_revision: int
    ledger_incarnation: str
    committed_mb: int


@dataclass(frozen=True)
class GPUResourceRuntimeObservation:
    status: Literal[
        "disabled",
        "missing",
        "prepared",
        "ready",
        "not_ready",
        "corrupt",
        "unavailable",
    ]
    reason: str
    ready: bool
    ledger_revision: int | None
    ledger_incarnation: str | None
    reconcile_deadline_ms: int | None
    committed_mb: int | None
    backend_count: int
    active_backend_count: int
    membership_state_counts: dict[str, int]
    allocation_state_counts: dict[str, int]
    lease_count: int | None
    card_queue_count: int | None
    backend_queue_count: int | None
    transition_present: bool | None
    durable_domain_matches: bool | None


async def repair_gpu_resource(
    data_provider: GPUAuthorityDataProvider,
    store: GPUArbiterStore,
    resource_id: str,
    allocatable_mb: int | None,
    *,
    config: Settings,
    force_proof_reset: bool = False,
    readiness_blocker: str | None = None,
) -> GPUResourceRepairResult:
    """Bootstrap or repair one desired-enforce resource without backend HTTP.

    A valid ready ledger may consume fresh idle health to extend its deadline
    without rebuilding live state. Continuity loss uses the strict proof reset.
    """

    validate_gpu_resource_id(resource_id)
    context = await store.prepared_proof_reset(resource_id)
    if allocatable_mb is None:
        if readiness_blocker is None or force_proof_reset:
            raise ValueError(
                "allocatable_mb is required except for a readiness blocker"
            )
        if context is not None:
            return GPUResourceRepairResult(
                resource_id=resource_id,
                action="resume_prepared",
                status="prepared",
                ready=False,
                reason="gpu_arbiter_proof_reset_in_progress",
                ledger_revision=context.ledger_revision,
                ledger_incarnation=context.ledger_incarnation,
                committed_mb=0,
            )
        allocatable_mb = (await store.snapshot(resource_id)).allocatable_mb
    if isinstance(allocatable_mb, bool) or not isinstance(allocatable_mb, int):
        raise ValueError("allocatable_mb must be a positive integer")
    if allocatable_mb <= 0:
        raise ValueError("allocatable_mb must be a positive integer")
    if not isinstance(force_proof_reset, bool):
        raise ValueError("force_proof_reset must be a boolean")
    if readiness_blocker is not None and (
        not isinstance(readiness_blocker, str)
        or not readiness_blocker
        or len(readiness_blocker) > 256
    ):
        raise ValueError("readiness_blocker must be a non-empty short string")

    action: Literal["already_ready", "bootstrap", "repair", "resume_prepared"] = (
        "resume_prepared" if context is not None else "repair"
    )
    begin_result: GPUReconcileResult | None = None

    if context is None and readiness_blocker is not None:
        blocked = await store.mark_card_not_ready(
            resource_id,
            allocatable_mb,
            reason=readiness_blocker,
        )
        return GPUResourceRepairResult(
            resource_id=resource_id,
            action="repair",
            status=blocked.status,
            ready=False,
            reason=blocked.reason,
            ledger_revision=blocked.ledger_revision,
            ledger_incarnation=blocked.ledger_incarnation,
            committed_mb=blocked.committed_mb,
        )

    if context is None:
        async with data_provider.transaction() as data:
            reply = await data.step(
                "repair_resource",
                {
                    "resource_id": resource_id,
                    "allocatable_mb": allocatable_mb,
                    "force_proof_reset": force_proof_reset,
                    "readiness_blocker": readiness_blocker,
                },
            )
            locked = parse_gpu_proof_domain(reply)
            durable_domain = _gpu_domain_members(locked.memberships)
            snapshot: GPUCardSnapshot | None = None
            try:
                snapshot = await store.snapshot(resource_id)
            except GPUArbiterStoreError as exc:
                if str(exc) == "gpu_arbiter_proof_reset_in_progress":
                    action = "resume_prepared"
                else:
                    action = "bootstrap"

            if snapshot is not None:
                domain_matches = snapshot.backend_memberships == durable_domain
                if (
                    not force_proof_reset
                    and snapshot.ready
                    and snapshot.allocatable_mb == allocatable_mb
                    and domain_matches
                    and all(member.state != "retiring" for member in durable_domain)
                ):
                    configured = config.gpu_arbiter_resources.get(resource_id)
                    proof_context = GPUProofResetContext(
                        resource_id=resource_id,
                        allocatable_mb=allocatable_mb,
                        reset_id="",
                        begin_fingerprint="",
                        ledger_revision=snapshot.ledger_revision,
                        ledger_incarnation=snapshot.ledger_incarnation,
                        prepared_at_ms=snapshot.observed_at_ms,
                        backend_ids=snapshot.backend_ids,
                        active_backend_ids=snapshot.active_backend_ids,
                        backend_memberships=durable_domain,
                    )
                    evaluations = {
                        str(member.backend_registry_id): _evaluate_gpu_member_proof(
                            member,
                            locked.fences.get(member.backend_registry_id),
                            locked.registries.get(member.backend_registry_id),
                            context=proof_context,
                            db_now=locked.db_now,
                            evidence_ttl=_HEALTH_EVIDENCE_MAX_AGE,
                            residency_cooldown_ms=0,
                        )
                        for member in locked.memberships
                    }
                    allocations = {item.backend_id: item for item in snapshot.allocations}
                    def agrees(backend_id: str, evaluation: _GPUProofEvaluation) -> bool:
                        actual = allocations.get(backend_id)
                        observed = evaluation.allocation
                        if observed is None:
                            return actual is None or actual.state in {
                                GPUAllocationState.UNLOADED,
                                GPUAllocationState.CPU_FALLBACK,
                            }
                        return (
                            actual is not None
                            and actual.state == GPUAllocationState.RESIDENT
                            and actual.generation == observed.generation
                            and actual.budget_mb == observed.budget_mb
                            and actual.max_concurrency == observed.max_concurrency
                            and actual.eviction_priority == observed.eviction_priority
                        )

                    complete = (
                        not config.gpu_arbiter_config_errors
                        and configured is not None
                        and configured.resource_id == resource_id
                        and configured.allocatable_mb == allocatable_mb
                        and bool(snapshot.active_backend_ids)
                        and len(locked.fences) == len(locked.memberships)
                        and all(
                            evaluation.complete
                            and evaluation.evidence_deadline_ms is not None
                            and agrees(backend_id, evaluation)
                            for backend_id, evaluation in evaluations.items()
                        )
                    )
                    if complete:
                        deadline = min(
                            evaluation.evidence_deadline_ms
                            for evaluation in evaluations.values()
                            if evaluation.evidence_deadline_ms is not None
                        )
                        if deadline > snapshot.reconcile_deadline_ms:
                            if not await store.renew_ready(
                                snapshot, evidence_deadline_ms=deadline
                            ):
                                return GPUResourceRepairResult(
                                    resource_id=resource_id,
                                    action="already_ready",
                                    status="stale_revision",
                                    ready=False,
                                    reason="readiness_renewal_cas_failed",
                                    ledger_revision=snapshot.ledger_revision,
                                    ledger_incarnation=snapshot.ledger_incarnation,
                                    committed_mb=snapshot.committed_mb,
                                )
                            return GPUResourceRepairResult(
                                resource_id=resource_id,
                                action="already_ready",
                                status="ready",
                                ready=True,
                                reason="",
                                ledger_revision=snapshot.ledger_revision + 1,
                                ledger_incarnation=snapshot.ledger_incarnation,
                                committed_mb=snapshot.committed_mb,
                            )
                    return GPUResourceRepairResult(
                        resource_id=resource_id,
                        action="already_ready",
                        status="ready",
                        ready=True,
                        reason="",
                        ledger_revision=snapshot.ledger_revision,
                        ledger_incarnation=snapshot.ledger_incarnation,
                        committed_mb=snapshot.committed_mb,
                    )
                expected_revision = snapshot.ledger_revision
                expected_incarnation = snapshot.ledger_incarnation
            else:
                cas = await store.proof_reset_cas(resource_id)
                expected_revision = cas.ledger_revision if cas is not None else None
                expected_incarnation = (
                    cas.ledger_incarnation if cas is not None else None
                )

            if action != "resume_prepared":
                reset_id = uuid.uuid4().hex
                begin_result = await store.begin_proof_reset(
                    resource_id,
                    allocatable_mb,
                    expected_ledger_revision=expected_revision,
                    expected_ledger_incarnation=expected_incarnation,
                    backend_memberships=durable_domain,
                    reset_id=reset_id,
                )
                if (
                    begin_result.status == "stale_revision"
                    and begin_result.reason == "proof_reset_cas_required"
                    and begin_result.ledger_revision > 0
                    and begin_result.ledger_incarnation
                ):
                    begin_result = await store.begin_proof_reset(
                        resource_id,
                        allocatable_mb,
                        expected_ledger_revision=begin_result.ledger_revision,
                        expected_ledger_incarnation=begin_result.ledger_incarnation,
                        backend_memberships=durable_domain,
                        reset_id=reset_id,
                    )

        context = await store.prepared_proof_reset(resource_id)
        if context is None:
            if begin_result is None:
                raise GPUArbiterStoreError("gpu_arbiter_proof_reset_in_progress")
            return GPUResourceRepairResult(
                resource_id=resource_id,
                action=action,
                status=begin_result.status,
                ready=begin_result.ready,
                reason=begin_result.reason,
                ledger_revision=begin_result.ledger_revision,
                ledger_incarnation=begin_result.ledger_incarnation,
                committed_mb=begin_result.committed_mb,
            )

    result = await commit_gpu_proof_reset_from_health(
        data_provider,
        store,
        context,
        config=config,
        readiness_blocker=readiness_blocker,
    )
    return GPUResourceRepairResult(
        resource_id=resource_id,
        action=action,
        status=result.status,
        ready=result.ready,
        reason=result.reason,
        ledger_revision=result.ledger_revision,
        ledger_incarnation=result.ledger_incarnation,
        committed_mb=result.committed_mb,
    )


def _runtime_memberships_from_native(
    value: object,
) -> tuple[GPUBackendDomainMember, ...]:
    if (
        type(value) is not dict
        or set(value) != {"status", "memberships"}
        or value["status"] != "ok"
        or type(value["memberships"]) is not list
    ):
        raise GPUAuthorityUnavailable()

    members: list[GPUBackendDomainMember] = []
    for item in value["memberships"]:
        if type(item) is not dict or set(item) != {
            "backend_id",
            "membership_epoch",
            "state",
        }:
            raise GPUAuthorityUnavailable()
        backend_id = item["backend_id"]
        epoch = item["membership_epoch"]
        state = item["state"]
        if (
            type(backend_id) is not str
            or type(epoch) is not int
            or epoch <= 0
            or epoch > 9_223_372_036_854_775_807
            or state not in {"pending", "active", "retiring"}
        ):
            raise GPUAuthorityUnavailable()
        try:
            parsed_backend_id = uuid.UUID(backend_id)
        except ValueError:
            raise GPUAuthorityUnavailable() from None
        if str(parsed_backend_id) != backend_id:
            raise GPUAuthorityUnavailable()
        members.append(
            GPUBackendDomainMember(
                backend_id=backend_id,
                membership_epoch=epoch,
                state=state,
            )
        )
    return tuple(members)


async def observe_gpu_resource_runtime(
    data_provider: GPUAuthorityDataProvider,
    store: GPUArbiterStore,
    resource_id: str,
) -> GPUResourceRuntimeObservation:
    """Read one resource for admin/task observability; never use it as proof."""

    async with data_provider.transaction() as data:
        native_memberships = await data.step(
            "resource_runtime_observation", {"resource_id": resource_id}
        )
    durable_memberships = _runtime_memberships_from_native(native_memberships)
    state_counts = {"pending": 0, "active": 0, "retiring": 0}
    for member in durable_memberships:
        state_counts[member.state] += 1
    try:
        prepared = await store.prepared_proof_reset(resource_id)
        if prepared is not None:
            return GPUResourceRuntimeObservation(
                status="prepared",
                reason="proof_reset_in_progress",
                ready=False,
                ledger_revision=prepared.ledger_revision,
                ledger_incarnation=prepared.ledger_incarnation,
                reconcile_deadline_ms=0,
                committed_mb=0,
                backend_count=len(prepared.backend_ids),
                active_backend_count=len(prepared.active_backend_ids),
                membership_state_counts=state_counts,
                allocation_state_counts={},
                lease_count=None,
                card_queue_count=None,
                backend_queue_count=None,
                transition_present=None,
                durable_domain_matches=(
                    prepared.backend_memberships == tuple(durable_memberships)
                ),
            )
        snapshot = await store.snapshot(resource_id)
    except GPUArbiterStoreError as exc:
        reason = str(exc)
        if reason == "gpu_arbiter_proof_reset_in_progress":
            prepared = await store.prepared_proof_reset(resource_id)
            if prepared is not None:
                return GPUResourceRuntimeObservation(
                    status="prepared",
                    reason=reason,
                    ready=False,
                    ledger_revision=prepared.ledger_revision,
                    ledger_incarnation=prepared.ledger_incarnation,
                    reconcile_deadline_ms=0,
                    committed_mb=0,
                    backend_count=len(prepared.backend_ids),
                    active_backend_count=len(prepared.active_backend_ids),
                    membership_state_counts=state_counts,
                    allocation_state_counts={},
                    lease_count=None,
                    card_queue_count=None,
                    backend_queue_count=None,
                    transition_present=None,
                    durable_domain_matches=(
                        prepared.backend_memberships == tuple(durable_memberships)
                    ),
                )
            status = "not_ready"
        elif reason == "gpu_arbiter_not_ready":
            status = "missing"
        elif reason == "gpu_arbiter_unavailable":
            status = "unavailable"
        else:
            status = "corrupt"
        return GPUResourceRuntimeObservation(
            status=status,
            reason=reason,
            ready=False,
            ledger_revision=None,
            ledger_incarnation=None,
            reconcile_deadline_ms=None,
            committed_mb=None,
            backend_count=0,
            active_backend_count=0,
            membership_state_counts=state_counts,
            allocation_state_counts={},
            lease_count=None,
            card_queue_count=None,
            backend_queue_count=None,
            transition_present=None,
            durable_domain_matches=None,
        )

    allocation_counts: dict[str, int] = defaultdict(int)
    for allocation in snapshot.allocations:
        allocation_counts[allocation.state.value] += 1
    return GPUResourceRuntimeObservation(
        status="ready" if snapshot.ready else "not_ready",
        reason="" if snapshot.ready else (snapshot.not_ready_reason or "not_ready"),
        ready=snapshot.ready,
        ledger_revision=snapshot.ledger_revision,
        ledger_incarnation=snapshot.ledger_incarnation,
        reconcile_deadline_ms=snapshot.reconcile_deadline_ms,
        committed_mb=snapshot.committed_mb,
        backend_count=len(snapshot.backend_ids),
        active_backend_count=len(snapshot.active_backend_ids),
        membership_state_counts=state_counts,
        allocation_state_counts=dict(sorted(allocation_counts.items())),
        lease_count=len(snapshot.leases),
        card_queue_count=snapshot.card_queue_count,
        backend_queue_count=snapshot.backend_queue_count,
        transition_present=snapshot.transition_present,
        durable_domain_matches=(
            snapshot.backend_memberships == tuple(durable_memberships)
        ),
    )


def disabled_gpu_resource_runtime_observation(
    durable_memberships: Sequence[GPUBackendDomainMember],
) -> GPUResourceRuntimeObservation:
    state_counts = {"pending": 0, "active": 0, "retiring": 0}
    for member in durable_memberships:
        state_counts[member.state] += 1
    return GPUResourceRuntimeObservation(
        status="disabled",
        reason="desired_mode_not_enforce",
        ready=False,
        ledger_revision=None,
        ledger_incarnation=None,
        reconcile_deadline_ms=None,
        committed_mb=None,
        backend_count=0,
        active_backend_count=0,
        membership_state_counts=state_counts,
        allocation_state_counts={},
        lease_count=None,
        card_queue_count=None,
        backend_queue_count=None,
        transition_present=None,
        durable_domain_matches=None,
    )
