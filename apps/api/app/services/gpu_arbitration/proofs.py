"""GPU proof schema, runtime subjects, and terminal commits.

Moved verbatim from the legacy aggregate ``gpu_arbiter.py``. This module owns the
canonical proof parser, residency parser, runtime subject dataclasses and errors,
generation preparation, token horizon, drain health classification, and the
cold / eviction / eviction-cancel terminal commits. It also holds the shared
proof-domain primitives (_snapshot_gpu_mode_backend, _lock_gpu_resource_proof_domain,
_optional_datetime_document, _gpu_domain_members) consumed by control_preparation,
reconciliation and retirement.

Depends on contracts, policy, fences, ledger, config DB models and SQLAlchemy.
Must not depend on ml_client or any high-level orchestration module.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field as dataclass_field
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
import uuid
from typing import Any, Literal

import structlog
from aap_protocol_v2.lifecycle import (
    managed_lifecycle_capability_sha256,
    validate_canonical_positive_int64,
)
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.gpu_backend_cancel_intent import GPUBackendCancelIntent
from app.db.models.gpu_backend_fence import GPUBackendFence
from app.db.models.gpu_backend_membership import GPUBackendMembership
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.gpu_arbitration.fences import (
    GPUFenceSessionFactory,
    _advance_gpu_backend_fence_in_transaction,
    _record_gpu_backend_token_expiry_in_transaction,
    _validate_token_expiry,
)
from app.services.gpu_arbitration.ledger import (
    GPU_EVICTION_OPERATION,
    GPUAllocation,
    GPUAllocationState,
    GPUArbiterStore,
    GPUBackendDomainMember,
    normalize_gpu_backend_max_concurrency,
)
from app.services.gpu_arbitration.policy import (
    _CANONICAL_POSITIVE_INT64_RE,
    _HEALTH_EVIDENCE_MAX_AGE,
    _MAX_POSITIVE_INT64,
)
from app.utils.gpu_resource import validate_gpu_resource_id


logger = structlog.get_logger(__name__)

_PROOF_RESET_MAX_WINDOW = timedelta(minutes=5)
_GPU_HEALTH_CHALLENGE_RE = re.compile(r"[0-9a-f]{64}\Z")
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
GPU_LEGACY_MODE_TOKEN_TTL_SECONDS = 30


class GPUResidentRuntimeSubjectError(RuntimeError):
    """Durable/runtime evidence cannot authorize one Resident-only workload."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GPUColdRuntimeSubjectError(RuntimeError):
    """Durable/runtime evidence cannot authorize one cold workload generation."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GPUIdleEvictionRuntimeSubjectError(RuntimeError):
    """Durable/runtime evidence cannot authorize one idle victim eviction."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GPUBusyEvictionRuntimeSubjectError(RuntimeError):
    """Durable/runtime evidence cannot authorize one busy-capable victim."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GPUEvictionCancelRuntimeSubjectError(RuntimeError):
    """Durable state cannot authorize one exact busy-drain cancellation."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _GPUProofInvalid(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _GPUProofProbe:
    raw: dict[str, Any]
    probe_started_at: datetime
    observed_at: datetime
    managed_lifecycle_sha256: str | None


@dataclass(frozen=True)
class _GPUProofResidency:
    raw: dict[str, Any]
    state: str
    gpu_loaded: bool | None
    active_requests: int
    builders: int
    borrowers: int
    draining: bool
    evictable: bool
    generation: str | None
    pool_ids: tuple[str, ...]
    pool_residencies: tuple[bool | None, ...]
    boot_id: str
    lifecycle_gate: str
    control_epoch: str | None
    identity: dict[str, str] | None


@dataclass(frozen=True)
class _GPUProofEvaluation:
    allocation: GPUAllocation | None
    complete: bool
    reason: str
    evidence_deadline_ms: int | None
    probe_document: Any
    residency_document: Any


@dataclass(frozen=True)
class _LockedGPUProofDomain:
    memberships: tuple[GPUBackendMembership, ...]
    fences: dict[uuid.UUID, GPUBackendFence]
    registries: dict[uuid.UUID, MLBackendRegistry]
    db_now: datetime


@dataclass(frozen=True)
class GPUResidentRuntimeSubject:
    """Exact durable and live identity used for one Resident-only admission."""

    backend_registry_id: uuid.UUID
    gpu_resource_id: str
    membership_epoch: int
    budget_mb: int
    eviction_priority: int
    max_concurrency: int
    boot_id: str
    generation: str
    control_epoch: str
    runtime_epoch: str
    db_now: datetime


@dataclass(frozen=True)
class GPUColdRuntimeSubject:
    """Exact idle-unloaded identity used to reserve one new GPU generation."""

    backend_registry_id: uuid.UUID
    gpu_resource_id: str
    membership_epoch: int
    budget_mb: int
    eviction_priority: int
    max_concurrency: int
    boot_id: str
    observed_generation: str | None
    generation_high_water: int
    control_epoch: str
    runtime_epoch: str
    db_now: datetime


@dataclass(frozen=True)
class GPUPreparedColdRuntimeSubject:
    """Cold subject whose new generation and token horizon are durable."""

    backend_registry_id: uuid.UUID
    gpu_resource_id: str
    membership_epoch: int
    budget_mb: int
    eviction_priority: int
    max_concurrency: int
    boot_id: str
    observed_generation: str | None
    generation: str
    control_epoch: str
    runtime_epoch: str
    token_expires_at: datetime
    db_now: datetime


@dataclass(frozen=True)
class GPUIdleEvictionRuntimeSubject:
    """Exact Resident identity eligible for one idle or busy eviction."""

    backend: MLBackendRegistry = dataclass_field(repr=False, compare=False)
    backend_registry_id: uuid.UUID
    gpu_resource_id: str
    membership_epoch: int
    budget_mb: int
    eviction_priority: int
    max_concurrency: int
    boot_id: str
    generation: str
    generation_high_water: int
    pool_ids: tuple[str, ...]
    control_epoch: str
    runtime_epoch: str
    challenge: str
    require_idle: bool
    db_now: datetime


@dataclass(frozen=True)
class GPUPreparedIdleEvictionRuntimeSubject:
    """Victim whose drain generation and token horizon are durable."""

    backend: MLBackendRegistry = dataclass_field(repr=False, compare=False)
    backend_registry_id: uuid.UUID
    gpu_resource_id: str
    membership_epoch: int
    budget_mb: int
    eviction_priority: int
    max_concurrency: int
    boot_id: str
    source_generation: str
    generation: str
    pool_ids: tuple[str, ...]
    control_epoch: str
    runtime_epoch: str
    token_expires_at: datetime
    require_idle: bool
    db_now: datetime


@dataclass(frozen=True)
class GPUPreparedEvictionCancelRuntimeSubject:
    """Exact newer generation durably reserved for one drain cancellation."""

    backend: MLBackendRegistry = dataclass_field(repr=False, compare=False)
    backend_registry_id: uuid.UUID
    gpu_resource_id: str
    membership_epoch: int
    budget_mb: int
    eviction_priority: int
    max_concurrency: int
    boot_id: str
    source_generation: str
    drain_generation: str
    generation: str
    pool_ids: tuple[str, ...]
    control_epoch: str
    runtime_epoch: str
    owner_id: str
    operation: str
    owner_hard_deadline_ms: int
    drain_token_expires_at: datetime
    token_expires_at: datetime
    jti: str
    idempotent: bool
    db_now: datetime


@dataclass(frozen=True)
class GPUColdTerminalCommitResult:
    """Result of classifying and committing one exposed cold generation."""

    status: Literal["finalized", "stale", "rejected"]
    state: GPUAllocationState
    reason: str
    idempotent: bool = False


@dataclass(frozen=True)
class GPUEvictionCommitResult:
    """Result of committing one health-proven eviction phase."""

    status: Literal["finalized", "stale", "rejected"]
    state: GPUAllocationState
    reason: str
    idempotent: bool = False


@dataclass(frozen=True)
class GPUEvictionDrainHealth:
    """Trusted read-only classification of one exact draining residency."""

    status: Literal["draining_busy", "ready_to_unload", "uncertain"]
    reason: str
    active_requests: int | None = None
    builders: int | None = None
    borrowers: int | None = None


_GPU_PROBE_KEYS = frozenset(
    {
        "protocol_version",
        "challenge",
        "backend_registry_id",
        "gpu_resource_id",
        "membership_epoch",
        "membership_state",
        "managed_lifecycle_sha256",
        "probe_started_at",
        "observed_at",
    }
)
_GPU_RESIDENCY_KEYS = frozenset(
    {
        "state",
        "gpu_loaded",
        "active_requests",
        "builders",
        "borrowers",
        "draining",
        "evictable",
        "generation",
        "pools",
        "boot_id",
        "lifecycle_gate",
        "control_epoch",
        "identity",
    }
)
_GPU_POOL_RESIDENCY_KEYS = frozenset({"resident", "device", "provider"})
_GPU_RESIDENCY_IDENTITY_KEYS = frozenset(
    {"audience", "backend_registry_id", "gpu_resource_id"}
)
_GPU_RESIDENCY_STATES = frozenset(
    {"unloaded", "loading", "resident", "draining", "unloading", "unknown"}
)


def _canonical_proof_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _GPUProofInvalid("timestamp_timezone_invalid")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_canonical_proof_timestamp(value: Any) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise _GPUProofInvalid("timestamp_format_invalid")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise _GPUProofInvalid("timestamp_format_invalid") from exc
    if _canonical_proof_timestamp(parsed) != value:
        raise _GPUProofInvalid("timestamp_format_invalid")
    return parsed


def _datetime_to_epoch_ms(value: datetime) -> int:
    utc_value = value.astimezone(UTC)
    delta = utc_value - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _strict_nonnegative_int64(value: Any, *, reason: str) -> int:
    if type(value) is not int or value < 0 or value > _MAX_POSITIVE_INT64:
        raise _GPUProofInvalid(reason)
    return value


def _strict_bool_or_none(value: Any, *, reason: str) -> bool | None:
    if value is not None and type(value) is not bool:
        raise _GPUProofInvalid(reason)
    return value


def _strict_string_or_none(value: Any, *, reason: str) -> str | None:
    if value is not None and type(value) is not str:
        raise _GPUProofInvalid(reason)
    return value


def _registry_gpu_max_concurrency(extra_params: Any) -> int:
    if type(extra_params) is not dict:
        raise _GPUProofInvalid("registry_concurrency_invalid")
    raw = extra_params.get("max_concurrency", 4)
    if type(raw) is str and re.fullmatch(r"[1-9][0-9]{0,4}", raw) is not None:
        raw = int(raw)
    try:
        return normalize_gpu_backend_max_concurrency(raw)
    except ValueError as exc:
        raise _GPUProofInvalid("registry_concurrency_invalid") from exc


def _parse_canonical_positive_int64(value: Any, *, reason: str) -> str:
    if (
        type(value) is not str
        or _CANONICAL_POSITIVE_INT64_RE.fullmatch(value) is None
        or int(value) > _MAX_POSITIVE_INT64
    ):
        raise _GPUProofInvalid(reason)
    return value


def _parse_optional_canonical_positive_int64(value: Any, *, reason: str) -> str | None:
    if value is None:
        return None
    return _parse_canonical_positive_int64(value, reason=reason)


def _parse_gpu_proof_probe(value: Any) -> _GPUProofProbe:
    if type(value) is not dict or set(value) != _GPU_PROBE_KEYS:
        raise _GPUProofInvalid("probe_schema_invalid")
    if value["protocol_version"] != "1":
        raise _GPUProofInvalid("probe_protocol_invalid")
    challenge = value["challenge"]
    if (
        type(challenge) is not str
        or _GPU_HEALTH_CHALLENGE_RE.fullmatch(challenge) is None
    ):
        raise _GPUProofInvalid("probe_challenge_invalid")
    for field in (
        "backend_registry_id",
        "gpu_resource_id",
        "membership_state",
    ):
        if type(value[field]) is not str:
            raise _GPUProofInvalid(f"probe_{field}_invalid")
    _parse_canonical_positive_int64(
        value["membership_epoch"], reason="probe_membership_epoch_invalid"
    )
    managed_lifecycle_sha256 = value["managed_lifecycle_sha256"]
    if managed_lifecycle_sha256 is not None and (
        type(managed_lifecycle_sha256) is not str
        or _SHA256_HEX_RE.fullmatch(managed_lifecycle_sha256) is None
    ):
        raise _GPUProofInvalid("probe_managed_lifecycle_sha256_invalid")
    probe_started_at = _parse_canonical_proof_timestamp(value["probe_started_at"])
    observed_at = _parse_canonical_proof_timestamp(value["observed_at"])
    if probe_started_at >= observed_at:
        raise _GPUProofInvalid("probe_clock_order_invalid")
    return _GPUProofProbe(
        raw=dict(value),
        probe_started_at=probe_started_at,
        observed_at=observed_at,
        managed_lifecycle_sha256=managed_lifecycle_sha256,
    )


def _health_managed_lifecycle_sha256(raw_health: Any) -> str | None:
    if type(raw_health) is not dict:
        return None
    raw_capabilities = raw_health.get("capabilities")
    if raw_capabilities is None:
        return None
    if type(raw_capabilities) is not dict:
        raise _GPUProofInvalid("managed_lifecycle_capabilities_invalid")
    raw_managed_lifecycle = raw_capabilities.get("managed_lifecycle")
    if raw_managed_lifecycle is None:
        return None
    try:
        return managed_lifecycle_capability_sha256(raw_managed_lifecycle)
    except (TypeError, ValueError) as exc:
        raise _GPUProofInvalid("managed_lifecycle_capability_invalid") from exc


def _parse_gpu_proof_residency(value: Any) -> _GPUProofResidency:
    if type(value) is not dict or set(value) != _GPU_RESIDENCY_KEYS:
        raise _GPUProofInvalid("residency_schema_invalid")

    state = value["state"]
    if type(state) is not str or state not in _GPU_RESIDENCY_STATES:
        raise _GPUProofInvalid("residency_state_invalid")
    gpu_loaded = _strict_bool_or_none(
        value["gpu_loaded"], reason="residency_gpu_loaded_invalid"
    )
    counters = {
        field: _strict_nonnegative_int64(
            value[field], reason=f"residency_{field}_invalid"
        )
        for field in ("active_requests", "builders", "borrowers")
    }
    if type(value["draining"]) is not bool:
        raise _GPUProofInvalid("residency_draining_invalid")
    if type(value["evictable"]) is not bool:
        raise _GPUProofInvalid("residency_evictable_invalid")
    generation = _parse_optional_canonical_positive_int64(
        value["generation"], reason="residency_generation_invalid"
    )
    control_epoch = _parse_optional_canonical_positive_int64(
        value["control_epoch"], reason="residency_control_epoch_invalid"
    )
    boot_id = value["boot_id"]
    if type(boot_id) is not str or not boot_id or len(boot_id) > 128:
        raise _GPUProofInvalid("residency_boot_id_invalid")
    lifecycle_gate = value["lifecycle_gate"]
    if type(lifecycle_gate) is not str or lifecycle_gate not in {"legacy", "enforce"}:
        raise _GPUProofInvalid("residency_lifecycle_gate_invalid")

    pools = value["pools"]
    if type(pools) is not dict or not pools:
        raise _GPUProofInvalid("residency_pools_invalid")
    pool_residencies: list[bool | None] = []
    for pool_id, pool in pools.items():
        if type(pool_id) is not str or not pool_id or pool_id.strip() != pool_id:
            raise _GPUProofInvalid("residency_pool_id_invalid")
        if type(pool) is not dict or set(pool) != _GPU_POOL_RESIDENCY_KEYS:
            raise _GPUProofInvalid("residency_pool_schema_invalid")
        pool_residencies.append(
            _strict_bool_or_none(
                pool["resident"], reason="residency_pool_state_invalid"
            )
        )
        _strict_string_or_none(pool["device"], reason="residency_pool_device_invalid")
        _strict_string_or_none(
            pool["provider"], reason="residency_pool_provider_invalid"
        )

    identity_value = value["identity"]
    identity: dict[str, str] | None
    if identity_value is None:
        identity = None
    else:
        if (
            type(identity_value) is not dict
            or set(identity_value) != _GPU_RESIDENCY_IDENTITY_KEYS
            or any(type(item) is not str for item in identity_value.values())
            or identity_value["audience"] != "aap-gpu-lifecycle"
        ):
            raise _GPUProofInvalid("residency_identity_invalid")
        identity = dict(identity_value)

    if gpu_loaded is False and (
        counters["builders"] != 0
        or counters["borrowers"] != 0
        or any(item is not False for item in pool_residencies)
    ):
        raise _GPUProofInvalid("residency_unloaded_inconsistent")
    if value["evictable"] and (
        generation is None or identity is None or lifecycle_gate != "enforce"
    ):
        raise _GPUProofInvalid("residency_evictable_inconsistent")

    return _GPUProofResidency(
        raw=dict(value),
        state=state,
        gpu_loaded=gpu_loaded,
        active_requests=counters["active_requests"],
        builders=counters["builders"],
        borrowers=counters["borrowers"],
        draining=value["draining"],
        evictable=value["evictable"],
        generation=generation,
        pool_ids=tuple(sorted(pools)),
        pool_residencies=tuple(pool_residencies),
        boot_id=boot_id,
        lifecycle_gate=lifecycle_gate,
        control_epoch=control_epoch,
        identity=identity,
    )


def _validate_gpu_resident_runtime_subject(
    membership: GPUBackendMembership,
    fence: GPUBackendFence,
    registry: MLBackendRegistry,
    *,
    db_now: datetime,
    evidence_ttl: timedelta,
) -> GPUResidentRuntimeSubject:
    membership_epoch = _strict_nonnegative_int64(
        membership.membership_epoch,
        reason="membership_epoch_invalid",
    )
    if membership.state != "active" or membership_epoch == 0:
        raise _GPUProofInvalid("membership_not_active")
    generation_high_water = _strict_nonnegative_int64(
        fence.generation_high_water,
        reason="generation_high_water_invalid",
    )
    control_epoch_high_water = _strict_nonnegative_int64(
        fence.control_epoch_high_water,
        reason="control_epoch_high_water_invalid",
    )
    runtime_epoch_high_water = _strict_nonnegative_int64(
        fence.runtime_epoch_high_water,
        reason="runtime_epoch_high_water_invalid",
    )
    runtime_epoch_baseline = _strict_nonnegative_int64(
        membership.runtime_epoch_baseline,
        reason="runtime_epoch_baseline_invalid",
    )
    if runtime_epoch_high_water <= runtime_epoch_baseline:
        raise _GPUProofInvalid("active_runtime_epoch_invalid")
    horizon = fence.token_expiry_high_water
    if horizon is None:
        raise _GPUProofInvalid("token_horizon_missing")
    if horizon.tzinfo is None or horizon.utcoffset() is None:
        raise _GPUProofInvalid("token_horizon_invalid")

    if (
        registry.state != "connected"
        or registry.gpu_resource_id != membership.gpu_resource_id
        or registry.vram_budget_mb != membership.vram_budget_mb
        or registry.eviction_priority != membership.eviction_priority
        or _registry_gpu_max_concurrency(registry.extra_params)
        != membership.max_concurrency
    ):
        raise _GPUProofInvalid("registry_claim_mismatch")

    raw_health = registry.health_meta
    raw_probe = (
        raw_health.get("gpu_arbiter_probe") if type(raw_health) is dict else None
    )
    raw_residency = raw_health.get("residency") if type(raw_health) is dict else None
    probe = _parse_gpu_proof_probe(raw_probe)
    capability_sha256 = _health_managed_lifecycle_sha256(raw_health)
    if (
        capability_sha256 is None
        or probe.managed_lifecycle_sha256 is None
        or capability_sha256 != probe.managed_lifecycle_sha256
    ):
        raise _GPUProofInvalid("managed_lifecycle_capability_mismatch")
    if (
        probe.raw["backend_registry_id"] != str(membership.backend_registry_id)
        or probe.raw["gpu_resource_id"] != membership.gpu_resource_id
        or probe.raw["membership_epoch"] != str(membership_epoch)
        or probe.raw["membership_state"] != "active"
    ):
        raise _GPUProofInvalid("probe_membership_mismatch")
    if (
        registry.last_checked_at is None
        or registry.last_checked_at.tzinfo is None
        or registry.last_checked_at.utcoffset() is None
        or registry.last_checked_at.astimezone(UTC) != probe.observed_at
    ):
        raise _GPUProofInvalid("probe_registry_clock_mismatch")
    db_now_utc = db_now.astimezone(UTC)
    if probe.observed_at > db_now_utc:
        raise _GPUProofInvalid("probe_from_future")
    if db_now_utc - probe.observed_at > evidence_ttl:
        raise _GPUProofInvalid("probe_expired")

    residency = _parse_gpu_proof_residency(raw_residency)
    identity = residency.identity
    if (
        residency.state != "resident"
        or residency.gpu_loaded is not True
        or residency.lifecycle_gate != "enforce"
        or residency.draining
        or residency.generation is None
        or residency.control_epoch is None
        or identity is None
        or any(item is None for item in residency.pool_residencies)
        or not any(item is True for item in residency.pool_residencies)
    ):
        raise _GPUProofInvalid("resident_runtime_not_ready")
    if (
        identity["backend_registry_id"] != str(membership.backend_registry_id)
        or identity["gpu_resource_id"] != membership.gpu_resource_id
    ):
        raise _GPUProofInvalid("residency_identity_mismatch")
    if int(residency.generation) > generation_high_water:
        raise _GPUProofInvalid("residency_generation_ahead")
    if int(residency.control_epoch) != control_epoch_high_water:
        raise _GPUProofInvalid("residency_control_epoch_mismatch")

    return GPUResidentRuntimeSubject(
        backend_registry_id=membership.backend_registry_id,
        gpu_resource_id=membership.gpu_resource_id,
        membership_epoch=membership_epoch,
        budget_mb=membership.vram_budget_mb,
        eviction_priority=membership.eviction_priority,
        max_concurrency=membership.max_concurrency,
        boot_id=residency.boot_id,
        generation=residency.generation,
        control_epoch=residency.control_epoch,
        runtime_epoch=str(runtime_epoch_high_water),
        db_now=db_now,
    )


def _validate_gpu_idle_eviction_runtime_subject(
    membership: GPUBackendMembership,
    fence: GPUBackendFence,
    registry: MLBackendRegistry,
    *,
    db_now: datetime,
    evidence_ttl: timedelta,
    expected_generation: str,
    challenge: str,
    require_idle: bool = True,
) -> GPUIdleEvictionRuntimeSubject:
    resident = _validate_gpu_resident_runtime_subject(
        membership,
        fence,
        registry,
        db_now=db_now,
        evidence_ttl=evidence_ttl,
    )
    raw_health = registry.health_meta
    raw_probe = (
        raw_health.get("gpu_arbiter_probe") if type(raw_health) is dict else None
    )
    raw_residency = raw_health.get("residency") if type(raw_health) is dict else None
    probe = _parse_gpu_proof_probe(raw_probe)
    residency = _parse_gpu_proof_residency(raw_residency)
    reason_prefix = "idle_eviction" if require_idle else "busy_eviction"
    if probe.raw["challenge"] != challenge:
        raise _GPUProofInvalid(f"{reason_prefix}_challenge_mismatch")
    if residency.generation != expected_generation:
        raise _GPUProofInvalid(f"{reason_prefix}_generation_mismatch")
    if not residency.evictable or (
        require_idle
        and (
            residency.active_requests != 0
            or residency.builders != 0
            or residency.borrowers != 0
        )
    ):
        raise _GPUProofInvalid(f"{reason_prefix}_runtime_not_ready")
    generation_high_water = _strict_nonnegative_int64(
        fence.generation_high_water,
        reason="generation_high_water_invalid",
    )
    return GPUIdleEvictionRuntimeSubject(
        backend=_snapshot_gpu_mode_backend(registry),
        backend_registry_id=resident.backend_registry_id,
        gpu_resource_id=resident.gpu_resource_id,
        membership_epoch=resident.membership_epoch,
        budget_mb=resident.budget_mb,
        eviction_priority=resident.eviction_priority,
        max_concurrency=resident.max_concurrency,
        boot_id=resident.boot_id,
        generation=resident.generation,
        generation_high_water=generation_high_water,
        pool_ids=residency.pool_ids,
        control_epoch=resident.control_epoch,
        runtime_epoch=resident.runtime_epoch,
        challenge=challenge,
        require_idle=require_idle,
        db_now=resident.db_now,
    )


def _runtime_subject_identity(subject: GPUResidentRuntimeSubject) -> tuple[Any, ...]:
    return (
        subject.backend_registry_id,
        subject.gpu_resource_id,
        subject.membership_epoch,
        subject.budget_mb,
        subject.eviction_priority,
        subject.max_concurrency,
        subject.boot_id,
        subject.generation,
        subject.control_epoch,
        subject.runtime_epoch,
    )


def _idle_eviction_runtime_subject_identity(
    subject: GPUIdleEvictionRuntimeSubject,
) -> tuple[Any, ...]:
    return (
        subject.backend_registry_id,
        subject.backend.url,
        subject.backend.auth_method,
        subject.backend.auth_token,
        subject.backend.auth_revision or 0,
        subject.gpu_resource_id,
        subject.membership_epoch,
        subject.budget_mb,
        subject.eviction_priority,
        subject.max_concurrency,
        subject.boot_id,
        subject.generation,
        subject.generation_high_water,
        subject.pool_ids,
        subject.control_epoch,
        subject.runtime_epoch,
        subject.challenge,
        subject.require_idle,
    )


def _validate_gpu_cold_runtime_subject(
    membership: GPUBackendMembership,
    fence: GPUBackendFence,
    registry: MLBackendRegistry,
    *,
    db_now: datetime,
    evidence_ttl: timedelta,
    expected_challenge: str | None = None,
) -> GPUColdRuntimeSubject:
    membership_epoch = _strict_nonnegative_int64(
        membership.membership_epoch,
        reason="membership_epoch_invalid",
    )
    if membership.state != "active" or membership_epoch == 0:
        raise _GPUProofInvalid("membership_not_active")
    generation_high_water = _strict_nonnegative_int64(
        fence.generation_high_water,
        reason="generation_high_water_invalid",
    )
    control_epoch_high_water = _strict_nonnegative_int64(
        fence.control_epoch_high_water,
        reason="control_epoch_high_water_invalid",
    )
    runtime_epoch_high_water = _strict_nonnegative_int64(
        fence.runtime_epoch_high_water,
        reason="runtime_epoch_high_water_invalid",
    )
    runtime_epoch_baseline = _strict_nonnegative_int64(
        membership.runtime_epoch_baseline,
        reason="runtime_epoch_baseline_invalid",
    )
    if runtime_epoch_high_water <= runtime_epoch_baseline:
        raise _GPUProofInvalid("active_runtime_epoch_invalid")
    horizon = fence.token_expiry_high_water
    if horizon is None:
        raise _GPUProofInvalid("token_horizon_missing")
    if horizon.tzinfo is None or horizon.utcoffset() is None:
        raise _GPUProofInvalid("token_horizon_invalid")

    if (
        registry.state != "connected"
        or registry.gpu_resource_id != membership.gpu_resource_id
        or registry.vram_budget_mb != membership.vram_budget_mb
        or registry.eviction_priority != membership.eviction_priority
        or _registry_gpu_max_concurrency(registry.extra_params)
        != membership.max_concurrency
    ):
        raise _GPUProofInvalid("registry_claim_mismatch")

    raw_health = registry.health_meta
    raw_probe = (
        raw_health.get("gpu_arbiter_probe") if type(raw_health) is dict else None
    )
    raw_residency = raw_health.get("residency") if type(raw_health) is dict else None
    probe = _parse_gpu_proof_probe(raw_probe)
    if expected_challenge is not None and probe.raw["challenge"] != expected_challenge:
        raise _GPUProofInvalid("cold_runtime_challenge_mismatch")
    capability_sha256 = _health_managed_lifecycle_sha256(raw_health)
    if (
        capability_sha256 is None
        or probe.managed_lifecycle_sha256 is None
        or capability_sha256 != probe.managed_lifecycle_sha256
    ):
        raise _GPUProofInvalid("managed_lifecycle_capability_mismatch")
    if (
        probe.raw["backend_registry_id"] != str(membership.backend_registry_id)
        or probe.raw["gpu_resource_id"] != membership.gpu_resource_id
        or probe.raw["membership_epoch"] != str(membership_epoch)
        or probe.raw["membership_state"] != "active"
    ):
        raise _GPUProofInvalid("probe_membership_mismatch")
    if (
        registry.last_checked_at is None
        or registry.last_checked_at.tzinfo is None
        or registry.last_checked_at.utcoffset() is None
        or registry.last_checked_at.astimezone(UTC) != probe.observed_at
    ):
        raise _GPUProofInvalid("probe_registry_clock_mismatch")
    db_now_utc = db_now.astimezone(UTC)
    if probe.observed_at > db_now_utc:
        raise _GPUProofInvalid("probe_from_future")
    if db_now_utc - probe.observed_at > evidence_ttl:
        raise _GPUProofInvalid("probe_expired")
    residency = _parse_gpu_proof_residency(raw_residency)
    identity = residency.identity
    if (
        residency.state not in {"unloaded", "resident"}
        or residency.gpu_loaded is not False
        or residency.lifecycle_gate != "enforce"
        or residency.draining
        or residency.evictable
        or residency.active_requests != 0
        or residency.builders != 0
        or residency.borrowers != 0
        or residency.control_epoch is None
        or identity is None
        or any(item is not False for item in residency.pool_residencies)
    ):
        raise _GPUProofInvalid("cold_runtime_not_ready")
    if (
        identity["backend_registry_id"] != str(membership.backend_registry_id)
        or identity["gpu_resource_id"] != membership.gpu_resource_id
    ):
        raise _GPUProofInvalid("residency_identity_mismatch")
    if (
        residency.generation is not None
        and int(residency.generation) > generation_high_water
    ):
        raise _GPUProofInvalid("residency_generation_ahead")
    if int(residency.control_epoch) != control_epoch_high_water:
        raise _GPUProofInvalid("residency_control_epoch_mismatch")

    return GPUColdRuntimeSubject(
        backend_registry_id=membership.backend_registry_id,
        gpu_resource_id=membership.gpu_resource_id,
        membership_epoch=membership_epoch,
        budget_mb=membership.vram_budget_mb,
        eviction_priority=membership.eviction_priority,
        max_concurrency=membership.max_concurrency,
        boot_id=residency.boot_id,
        observed_generation=residency.generation,
        generation_high_water=generation_high_water,
        control_epoch=residency.control_epoch,
        runtime_epoch=str(runtime_epoch_high_water),
        db_now=db_now,
    )


def _cold_runtime_subject_identity(subject: GPUColdRuntimeSubject) -> tuple[Any, ...]:
    return (
        subject.backend_registry_id,
        subject.gpu_resource_id,
        subject.membership_epoch,
        subject.budget_mb,
        subject.eviction_priority,
        subject.max_concurrency,
        subject.boot_id,
        subject.observed_generation,
        subject.generation_high_water,
        subject.control_epoch,
        subject.runtime_epoch,
    )


def _validate_runtime_subject_evidence_ttl(evidence_ttl: timedelta) -> None:
    if evidence_ttl <= timedelta(0) or evidence_ttl > _PROOF_RESET_MAX_WINDOW:
        raise ValueError(
            "evidence_ttl must be positive and no greater than five minutes"
        )


def _validate_runtime_subject_inputs(
    backend_id: str,
    gpu_resource_id: str,
    evidence_ttl: timedelta,
    *,
    error_type: type[GPUResidentRuntimeSubjectError]
    | type[GPUColdRuntimeSubjectError]
    | type[GPUIdleEvictionRuntimeSubjectError]
    | type[GPUBusyEvictionRuntimeSubjectError]
    | type[GPUEvictionCancelRuntimeSubjectError] = GPUResidentRuntimeSubjectError,
) -> uuid.UUID:
    try:
        backend_registry_id = uuid.UUID(backend_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise error_type("backend_identity_invalid") from exc
    if str(backend_registry_id) != backend_id:
        raise error_type("backend_identity_invalid")
    try:
        validate_gpu_resource_id(gpu_resource_id)
    except (TypeError, ValueError) as exc:
        raise error_type("gpu_resource_id_invalid") from exc
    _validate_runtime_subject_evidence_ttl(evidence_ttl)
    return backend_registry_id


async def read_gpu_resident_runtime_subject(
    db: AsyncSession,
    *,
    backend_id: str,
    gpu_resource_id: str,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPUResidentRuntimeSubject:
    """Read one strict, non-idle Resident runtime subject from one MVCC snapshot."""

    backend_registry_id = _validate_runtime_subject_inputs(
        backend_id,
        gpu_resource_id,
        evidence_ttl,
    )
    row = (
        await db.execute(
            select(
                GPUBackendMembership,
                GPUBackendFence,
                MLBackendRegistry,
                func.clock_timestamp(),
            )
            .join(
                GPUBackendFence,
                GPUBackendFence.backend_registry_id
                == GPUBackendMembership.backend_registry_id,
            )
            .join(
                MLBackendRegistry,
                MLBackendRegistry.id == GPUBackendMembership.backend_registry_id,
            )
            .where(
                GPUBackendMembership.backend_registry_id == backend_registry_id,
                GPUBackendMembership.gpu_resource_id == gpu_resource_id,
                GPUBackendMembership.state == "active",
            )
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise GPUResidentRuntimeSubjectError("membership_not_active")
    membership, fence, registry, db_now = row
    if (
        not isinstance(db_now, datetime)
        or db_now.tzinfo is None
        or db_now.utcoffset() is None
    ):
        raise RuntimeError("PostgreSQL returned an invalid runtime proof clock")
    try:
        return _validate_gpu_resident_runtime_subject(
            membership,
            fence,
            registry,
            db_now=db_now,
            evidence_ttl=evidence_ttl,
        )
    except _GPUProofInvalid as exc:
        raise GPUResidentRuntimeSubjectError(exc.reason) from None


async def _read_gpu_eviction_runtime_subject(
    db: AsyncSession,
    *,
    backend_id: str,
    gpu_resource_id: str,
    expected_generation: str,
    challenge: str,
    evidence_ttl: timedelta,
    require_idle: bool,
    error_type: type[GPUIdleEvictionRuntimeSubjectError]
    | type[GPUBusyEvictionRuntimeSubjectError],
) -> GPUIdleEvictionRuntimeSubject:
    reason_prefix = "idle_eviction" if require_idle else "busy_eviction"
    backend_registry_id = _validate_runtime_subject_inputs(
        backend_id,
        gpu_resource_id,
        evidence_ttl,
        error_type=error_type,
    )
    try:
        validate_canonical_positive_int64(expected_generation)
    except (TypeError, ValueError) as exc:
        raise error_type(f"{reason_prefix}_generation_invalid") from exc
    if (
        not isinstance(challenge, str)
        or _GPU_HEALTH_CHALLENGE_RE.fullmatch(challenge) is None
    ):
        raise error_type(f"{reason_prefix}_challenge_invalid")
    row = (
        await db.execute(
            select(
                GPUBackendMembership,
                GPUBackendFence,
                MLBackendRegistry,
                func.clock_timestamp(),
            )
            .join(
                GPUBackendFence,
                GPUBackendFence.backend_registry_id
                == GPUBackendMembership.backend_registry_id,
            )
            .join(
                MLBackendRegistry,
                MLBackendRegistry.id == GPUBackendMembership.backend_registry_id,
            )
            .where(
                GPUBackendMembership.backend_registry_id == backend_registry_id,
                GPUBackendMembership.gpu_resource_id == gpu_resource_id,
                GPUBackendMembership.state == "active",
            )
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise error_type("membership_not_active")
    membership, fence, registry, db_now = row
    if (
        not isinstance(db_now, datetime)
        or db_now.tzinfo is None
        or db_now.utcoffset() is None
    ):
        raise RuntimeError("PostgreSQL returned an invalid runtime proof clock")
    try:
        return _validate_gpu_idle_eviction_runtime_subject(
            membership,
            fence,
            registry,
            db_now=db_now,
            evidence_ttl=evidence_ttl,
            expected_generation=expected_generation,
            challenge=challenge,
            require_idle=require_idle,
        )
    except _GPUProofInvalid as exc:
        raise error_type(exc.reason) from None


async def read_gpu_idle_eviction_runtime_subject(
    db: AsyncSession,
    *,
    backend_id: str,
    gpu_resource_id: str,
    expected_generation: str,
    challenge: str,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPUIdleEvictionRuntimeSubject:
    """Read one strict idle Resident victim from one MVCC snapshot."""

    return await _read_gpu_eviction_runtime_subject(
        db,
        backend_id=backend_id,
        gpu_resource_id=gpu_resource_id,
        expected_generation=expected_generation,
        challenge=challenge,
        evidence_ttl=evidence_ttl,
        require_idle=True,
        error_type=GPUIdleEvictionRuntimeSubjectError,
    )


async def read_gpu_busy_eviction_runtime_subject(
    db: AsyncSession,
    *,
    backend_id: str,
    gpu_resource_id: str,
    expected_generation: str,
    challenge: str,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPUIdleEvictionRuntimeSubject:
    """Read one strict Resident victim while allowing live work to drain."""

    return await _read_gpu_eviction_runtime_subject(
        db,
        backend_id=backend_id,
        gpu_resource_id=gpu_resource_id,
        expected_generation=expected_generation,
        challenge=challenge,
        evidence_ttl=evidence_ttl,
        require_idle=False,
        error_type=GPUBusyEvictionRuntimeSubjectError,
    )


async def read_gpu_cold_runtime_subject(
    db: AsyncSession,
    *,
    backend_id: str,
    gpu_resource_id: str,
    expected_challenge: str | None = None,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPUColdRuntimeSubject:
    """Read one strict idle-unloaded subject for a fenced cold generation."""

    backend_registry_id = _validate_runtime_subject_inputs(
        backend_id,
        gpu_resource_id,
        evidence_ttl,
        error_type=GPUColdRuntimeSubjectError,
    )
    if expected_challenge is not None and (
        not isinstance(expected_challenge, str)
        or _GPU_HEALTH_CHALLENGE_RE.fullmatch(expected_challenge) is None
    ):
        raise GPUColdRuntimeSubjectError("cold_runtime_challenge_invalid")
    row = (
        await db.execute(
            select(
                GPUBackendMembership,
                GPUBackendFence,
                MLBackendRegistry,
                func.clock_timestamp(),
            )
            .join(
                GPUBackendFence,
                GPUBackendFence.backend_registry_id
                == GPUBackendMembership.backend_registry_id,
            )
            .join(
                MLBackendRegistry,
                MLBackendRegistry.id == GPUBackendMembership.backend_registry_id,
            )
            .where(
                GPUBackendMembership.backend_registry_id == backend_registry_id,
                GPUBackendMembership.gpu_resource_id == gpu_resource_id,
                GPUBackendMembership.state == "active",
            )
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise GPUColdRuntimeSubjectError("membership_not_active")
    membership, fence, registry, db_now = row
    if (
        not isinstance(db_now, datetime)
        or db_now.tzinfo is None
        or db_now.utcoffset() is None
    ):
        raise RuntimeError("PostgreSQL returned an invalid runtime proof clock")
    try:
        return _validate_gpu_cold_runtime_subject(
            membership,
            fence,
            registry,
            db_now=db_now,
            evidence_ttl=evidence_ttl,
            expected_challenge=expected_challenge,
        )
    except _GPUProofInvalid as exc:
        raise GPUColdRuntimeSubjectError(exc.reason) from None


async def _lock_gpu_idle_eviction_runtime_subject(
    db: AsyncSession,
    expected_subject: GPUIdleEvictionRuntimeSubject,
    *,
    evidence_ttl: timedelta,
) -> GPUIdleEvictionRuntimeSubject:
    error_type = (
        GPUIdleEvictionRuntimeSubjectError
        if expected_subject.require_idle
        else GPUBusyEvictionRuntimeSubjectError
    )
    locked = await _lock_gpu_resource_proof_domain(
        db,
        expected_subject.gpu_resource_id,
    )
    membership = next(
        (
            item
            for item in locked.memberships
            if item.backend_registry_id == expected_subject.backend_registry_id
            and item.membership_epoch == expected_subject.membership_epoch
            and item.state == "active"
        ),
        None,
    )
    fence = locked.fences.get(expected_subject.backend_registry_id)
    registry = locked.registries.get(expected_subject.backend_registry_id)
    if membership is None or fence is None or registry is None:
        raise error_type("runtime_subject_missing")
    try:
        current_subject = _validate_gpu_idle_eviction_runtime_subject(
            membership,
            fence,
            registry,
            db_now=locked.db_now,
            evidence_ttl=evidence_ttl,
            expected_generation=expected_subject.generation,
            challenge=expected_subject.challenge,
            require_idle=expected_subject.require_idle,
        )
    except _GPUProofInvalid as exc:
        raise error_type(exc.reason) from None
    if _idle_eviction_runtime_subject_identity(
        current_subject
    ) != _idle_eviction_runtime_subject_identity(expected_subject):
        raise error_type("runtime_subject_changed")
    return current_subject


async def prepare_gpu_idle_eviction_runtime_generation(
    session_factory: GPUFenceSessionFactory,
    expected_subject: GPUIdleEvictionRuntimeSubject,
    *,
    token_expires_at: datetime,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPUPreparedIdleEvictionRuntimeSubject:
    """Atomically advance one exact victim generation and token horizon."""

    _validate_token_expiry(token_expires_at)
    _validate_runtime_subject_evidence_ttl(evidence_ttl)
    async with session_factory() as db:
        async with db.begin():
            current_subject = await _lock_gpu_idle_eviction_runtime_subject(
                db,
                expected_subject,
                evidence_ttl=evidence_ttl,
            )
            if token_expires_at <= current_subject.db_now:
                error_type = (
                    GPUIdleEvictionRuntimeSubjectError
                    if current_subject.require_idle
                    else GPUBusyEvictionRuntimeSubjectError
                )
                raise error_type("token_expiry_not_in_future")
            generation = await _advance_gpu_backend_fence_in_transaction(
                db,
                expected_subject.backend_registry_id,
                "generation",
                gpu_resource_id=expected_subject.gpu_resource_id,
                membership_epoch=expected_subject.membership_epoch,
                token_expires_at=token_expires_at,
            )
    return GPUPreparedIdleEvictionRuntimeSubject(
        backend=current_subject.backend,
        backend_registry_id=current_subject.backend_registry_id,
        gpu_resource_id=current_subject.gpu_resource_id,
        membership_epoch=current_subject.membership_epoch,
        budget_mb=current_subject.budget_mb,
        eviction_priority=current_subject.eviction_priority,
        max_concurrency=current_subject.max_concurrency,
        boot_id=current_subject.boot_id,
        source_generation=current_subject.generation,
        generation=str(generation),
        pool_ids=current_subject.pool_ids,
        control_epoch=current_subject.control_epoch,
        runtime_epoch=current_subject.runtime_epoch,
        token_expires_at=token_expires_at,
        require_idle=current_subject.require_idle,
        db_now=current_subject.db_now,
    )


def _eviction_cancel_source_fingerprint(
    subject: GPUPreparedIdleEvictionRuntimeSubject,
) -> str:
    payload = {
        "backend_registry_id": str(subject.backend_registry_id),
        "backend_url": subject.backend.url,
        "backend_auth_method": subject.backend.auth_method,
        "backend_auth_token": subject.backend.auth_token,
        "gpu_resource_id": subject.gpu_resource_id,
        "membership_epoch": subject.membership_epoch,
        "budget_mb": subject.budget_mb,
        "eviction_priority": subject.eviction_priority,
        "max_concurrency": subject.max_concurrency,
        "boot_id": subject.boot_id,
        "source_generation": subject.source_generation,
        "drain_generation": subject.generation,
        "pool_ids": list(subject.pool_ids),
        "control_epoch": subject.control_epoch,
        "runtime_epoch": subject.runtime_epoch,
        "token_expires_at": _canonical_proof_timestamp(subject.token_expires_at),
        "require_idle": subject.require_idle,
    }
    # Existing durable cancel intents predate credential rotation. Preserve their
    # revision-zero fingerprint so repair can resume them after this migration.
    if subject.backend.auth_revision:
        payload["backend_auth_revision"] = subject.backend.auth_revision
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_eviction_cancel_intent_pool_ids(
    intent: GPUBackendCancelIntent,
) -> tuple[str, ...]:
    value = intent.pool_ids
    if (
        type(value) is not list
        or not value
        or any(
            type(pool_id) is not str or not pool_id or pool_id.strip() != pool_id
            for pool_id in value
        )
    ):
        raise _GPUProofInvalid("cancel_intent_pool_ids_invalid")
    pool_ids = tuple(value)
    if pool_ids != tuple(sorted(set(pool_ids))):
        raise _GPUProofInvalid("cancel_intent_pool_ids_invalid")
    return pool_ids


def _validate_eviction_cancel_intent_durable_state(
    intent: GPUBackendCancelIntent,
    fence: GPUBackendFence,
    *,
    db_now: datetime,
) -> tuple[str, ...]:
    try:
        source_generation = _strict_nonnegative_int64(
            intent.source_generation,
            reason="cancel_intent_generation_invalid",
        )
        drain_generation = _strict_nonnegative_int64(
            intent.drain_generation,
            reason="cancel_intent_generation_invalid",
        )
        generation = _strict_nonnegative_int64(
            intent.generation,
            reason="cancel_intent_generation_invalid",
        )
        membership_epoch = _strict_nonnegative_int64(
            intent.membership_epoch,
            reason="cancel_intent_membership_invalid",
        )
        control_epoch = _strict_nonnegative_int64(
            intent.control_epoch,
            reason="cancel_intent_epoch_invalid",
        )
        runtime_epoch = _strict_nonnegative_int64(
            intent.runtime_epoch,
            reason="cancel_intent_epoch_invalid",
        )
        durable_generation = _strict_nonnegative_int64(
            fence.generation_high_water,
            reason="generation_high_water_invalid",
        )
        durable_control_epoch = _strict_nonnegative_int64(
            fence.control_epoch_high_water,
            reason="control_epoch_high_water_invalid",
        )
        durable_runtime_epoch = _strict_nonnegative_int64(
            fence.runtime_epoch_high_water,
            reason="runtime_epoch_high_water_invalid",
        )
    except _GPUProofInvalid:
        raise
    if (
        source_generation <= 0
        or drain_generation <= source_generation
        or generation <= drain_generation
        or membership_epoch <= 0
        or control_epoch <= 0
        or runtime_epoch <= 0
    ):
        raise _GPUProofInvalid("cancel_intent_generation_invalid")
    if (
        durable_generation != generation
        or durable_control_epoch != control_epoch
        or durable_runtime_epoch != runtime_epoch
    ):
        raise _GPUProofInvalid("cancel_intent_fence_changed")
    if (
        intent.operation != GPU_EVICTION_OPERATION
        or not isinstance(intent.owner_id, str)
        or not intent.owner_id
        or len(intent.owner_id) > 256
        or not isinstance(intent.jti, str)
        or not intent.jti.startswith("transition:")
        or len(intent.jti) > 256
        or not isinstance(intent.boot_id, str)
        or not intent.boot_id
        or len(intent.boot_id) > 128
        or not isinstance(intent.subject_fingerprint, str)
        or _SHA256_HEX_RE.fullmatch(intent.subject_fingerprint) is None
    ):
        raise _GPUProofInvalid("cancel_intent_identity_invalid")
    try:
        validate_gpu_resource_id(intent.gpu_resource_id)
    except (TypeError, ValueError) as exc:
        raise _GPUProofInvalid("cancel_intent_identity_invalid") from exc
    if (
        intent.drain_token_expires_at.tzinfo is None
        or intent.drain_token_expires_at.utcoffset() is None
        or intent.token_expires_at.tzinfo is None
        or intent.token_expires_at.utcoffset() is None
    ):
        raise _GPUProofInvalid("cancel_intent_token_horizon_invalid")
    try:
        owner_hard_deadline = datetime.fromtimestamp(
            intent.owner_hard_deadline_ms / 1000,
            UTC,
        )
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise _GPUProofInvalid("cancel_intent_owner_deadline_invalid") from exc
    if (
        isinstance(intent.owner_hard_deadline_ms, bool)
        or not isinstance(intent.owner_hard_deadline_ms, int)
        or intent.owner_hard_deadline_ms <= 0
        or owner_hard_deadline <= db_now
        or intent.token_expires_at.astimezone(UTC) > owner_hard_deadline
    ):
        raise _GPUProofInvalid("cancel_intent_owner_deadline_invalid")
    if intent.token_expires_at.astimezone(UTC) <= db_now.astimezone(UTC):
        raise _GPUProofInvalid("cancel_intent_expired")
    horizon = fence.token_expiry_high_water
    if (
        horizon is None
        or horizon.tzinfo is None
        or horizon.utcoffset() is None
        or horizon.astimezone(UTC)
        < max(
            intent.drain_token_expires_at.astimezone(UTC),
            intent.token_expires_at.astimezone(UTC),
        )
    ):
        raise _GPUProofInvalid("cancel_intent_token_horizon_invalid")
    return _validated_eviction_cancel_intent_pool_ids(intent)


def _prepared_eviction_cancel_subject(
    source: GPUPreparedIdleEvictionRuntimeSubject,
    intent: GPUBackendCancelIntent,
    *,
    db_now: datetime,
    idempotent: bool,
) -> GPUPreparedEvictionCancelRuntimeSubject:
    return GPUPreparedEvictionCancelRuntimeSubject(
        backend=source.backend,
        backend_registry_id=source.backend_registry_id,
        gpu_resource_id=source.gpu_resource_id,
        membership_epoch=source.membership_epoch,
        budget_mb=source.budget_mb,
        eviction_priority=source.eviction_priority,
        max_concurrency=source.max_concurrency,
        boot_id=source.boot_id,
        source_generation=str(intent.source_generation),
        drain_generation=str(intent.drain_generation),
        generation=str(intent.generation),
        pool_ids=_validated_eviction_cancel_intent_pool_ids(intent),
        control_epoch=source.control_epoch,
        runtime_epoch=source.runtime_epoch,
        owner_id=intent.owner_id,
        operation=intent.operation,
        owner_hard_deadline_ms=intent.owner_hard_deadline_ms,
        drain_token_expires_at=intent.drain_token_expires_at,
        token_expires_at=intent.token_expires_at,
        jti=intent.jti,
        idempotent=idempotent,
        db_now=db_now,
    )


async def prepare_gpu_eviction_cancel_runtime_generation(
    session_factory: GPUFenceSessionFactory,
    expected_subject: GPUPreparedIdleEvictionRuntimeSubject,
    *,
    owner_id: str,
    owner_hard_deadline_ms: int,
    token_expires_at: datetime,
) -> GPUPreparedEvictionCancelRuntimeSubject:
    """Persist or exactly replay one newer busy-drain cancellation generation."""

    if expected_subject.require_idle:
        raise GPUEvictionCancelRuntimeSubjectError("busy_cancel_required")
    if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 256:
        raise ValueError("owner_id must be a non-empty string up to 256 chars")
    if (
        isinstance(owner_hard_deadline_ms, bool)
        or not isinstance(owner_hard_deadline_ms, int)
        or owner_hard_deadline_ms <= 0
        or owner_hard_deadline_ms > _MAX_POSITIVE_INT64
    ):
        raise ValueError("owner_hard_deadline_ms must be a positive int64")
    _validate_token_expiry(token_expires_at)
    fingerprint = _eviction_cancel_source_fingerprint(expected_subject)

    async with session_factory() as db:
        async with db.begin():
            locked = await _lock_gpu_resource_proof_domain(
                db,
                expected_subject.gpu_resource_id,
            )
            membership = next(
                (
                    item
                    for item in locked.memberships
                    if item.backend_registry_id == expected_subject.backend_registry_id
                    and item.membership_epoch == expected_subject.membership_epoch
                    and item.state == "active"
                ),
                None,
            )
            fence = locked.fences.get(expected_subject.backend_registry_id)
            registry = locked.registries.get(expected_subject.backend_registry_id)
            if membership is None or fence is None or registry is None:
                raise GPUEvictionCancelRuntimeSubjectError("runtime_subject_missing")
            try:
                durable_reason = _eviction_terminal_durable_reason(
                    membership,
                    fence,
                    registry,
                    expected_subject,
                )
            except _GPUProofInvalid as exc:
                durable_reason = exc.reason
            if durable_reason is not None:
                raise GPUEvictionCancelRuntimeSubjectError(durable_reason)
            if token_expires_at <= locked.db_now:
                raise GPUEvictionCancelRuntimeSubjectError("token_expiry_not_in_future")
            try:
                owner_hard_deadline = datetime.fromtimestamp(
                    owner_hard_deadline_ms / 1000,
                    UTC,
                )
            except (OSError, OverflowError, ValueError) as exc:
                raise ValueError(
                    "owner_hard_deadline_ms must fit a UTC datetime"
                ) from exc
            if owner_hard_deadline <= locked.db_now:
                raise GPUEvictionCancelRuntimeSubjectError(
                    "transition_owner_deadline_expired"
                )
            if token_expires_at.astimezone(UTC) > owner_hard_deadline:
                raise GPUEvictionCancelRuntimeSubjectError(
                    "token_expiry_exceeds_owner_deadline"
                )

            intent = await db.scalar(
                select(GPUBackendCancelIntent)
                .where(
                    GPUBackendCancelIntent.backend_registry_id
                    == expected_subject.backend_registry_id
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            durable_generation = _strict_nonnegative_int64(
                fence.generation_high_water,
                reason="generation_high_water_invalid",
            )
            expected_drain_generation = int(expected_subject.generation)
            if (
                intent is not None
                and intent.drain_generation == expected_drain_generation
            ):
                try:
                    intent_pool_ids = _validate_eviction_cancel_intent_durable_state(
                        intent,
                        fence,
                        db_now=locked.db_now,
                    )
                except _GPUProofInvalid as exc:
                    raise GPUEvictionCancelRuntimeSubjectError(exc.reason) from None
                exact = (
                    intent.gpu_resource_id == expected_subject.gpu_resource_id
                    and intent.membership_epoch == expected_subject.membership_epoch
                    and intent.boot_id == expected_subject.boot_id
                    and intent.control_epoch == int(expected_subject.control_epoch)
                    and intent.runtime_epoch == int(expected_subject.runtime_epoch)
                    and intent.source_generation
                    == int(expected_subject.source_generation)
                    and intent.owner_id == owner_id
                    and intent.operation == GPU_EVICTION_OPERATION
                    and intent.owner_hard_deadline_ms == owner_hard_deadline_ms
                    and intent.drain_token_expires_at.astimezone(UTC)
                    == expected_subject.token_expires_at.astimezone(UTC)
                    and intent.token_expires_at.astimezone(UTC)
                    == token_expires_at.astimezone(UTC)
                    and intent_pool_ids == expected_subject.pool_ids
                    and intent.subject_fingerprint == fingerprint
                )
                if not exact:
                    raise GPUEvictionCancelRuntimeSubjectError("cancel_intent_conflict")
                return _prepared_eviction_cancel_subject(
                    expected_subject,
                    intent,
                    db_now=locked.db_now,
                    idempotent=True,
                )

            if durable_generation < expected_drain_generation:
                raise GPUEvictionCancelRuntimeSubjectError("generation_changed")
            if intent is not None and intent.generation >= expected_drain_generation:
                raise GPUEvictionCancelRuntimeSubjectError("cancel_intent_corrupt")
            generation = await _advance_gpu_backend_fence_in_transaction(
                db,
                expected_subject.backend_registry_id,
                "generation",
                gpu_resource_id=expected_subject.gpu_resource_id,
                membership_epoch=expected_subject.membership_epoch,
                token_expires_at=token_expires_at,
            )
            values = {
                "gpu_resource_id": expected_subject.gpu_resource_id,
                "membership_epoch": expected_subject.membership_epoch,
                "boot_id": expected_subject.boot_id,
                "control_epoch": int(expected_subject.control_epoch),
                "runtime_epoch": int(expected_subject.runtime_epoch),
                "source_generation": int(expected_subject.source_generation),
                "drain_generation": expected_drain_generation,
                "generation": generation,
                "owner_id": owner_id,
                "operation": GPU_EVICTION_OPERATION,
                "owner_hard_deadline_ms": owner_hard_deadline_ms,
                "drain_token_expires_at": expected_subject.token_expires_at,
                "token_expires_at": token_expires_at,
                "jti": f"transition:{uuid.uuid4()}",
                "pool_ids": list(expected_subject.pool_ids),
                "subject_fingerprint": fingerprint,
            }
            if intent is None:
                intent = GPUBackendCancelIntent(
                    backend_registry_id=expected_subject.backend_registry_id,
                    **values,
                )
                db.add(intent)
            else:
                for field, value in values.items():
                    setattr(intent, field, value)
                intent.updated_at = func.now()
            await db.flush()
            return _prepared_eviction_cancel_subject(
                expected_subject,
                intent,
                db_now=locked.db_now,
                idempotent=False,
            )


async def read_gpu_eviction_cancel_runtime_subject(
    session_factory: GPUFenceSessionFactory,
    *,
    backend_id: str,
    gpu_resource_id: str,
    owner_id: str,
) -> GPUPreparedEvictionCancelRuntimeSubject:
    """Recover one still-live exact cancel intent without its in-memory source."""

    backend_registry_id = _validate_runtime_subject_inputs(
        backend_id,
        gpu_resource_id,
        _HEALTH_EVIDENCE_MAX_AGE,
        error_type=GPUEvictionCancelRuntimeSubjectError,
    )
    if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 256:
        raise ValueError("owner_id must be a non-empty string up to 256 chars")
    async with session_factory() as db:
        async with db.begin():
            locked = await _lock_gpu_resource_proof_domain(db, gpu_resource_id)
            membership = next(
                (
                    item
                    for item in locked.memberships
                    if item.backend_registry_id == backend_registry_id
                    and item.state == "active"
                ),
                None,
            )
            fence = locked.fences.get(backend_registry_id)
            registry = locked.registries.get(backend_registry_id)
            intent = await db.scalar(
                select(GPUBackendCancelIntent)
                .where(
                    GPUBackendCancelIntent.backend_registry_id == backend_registry_id
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                membership is None
                or fence is None
                or registry is None
                or intent is None
            ):
                raise GPUEvictionCancelRuntimeSubjectError("cancel_intent_missing")
            try:
                pool_ids = _validate_eviction_cancel_intent_durable_state(
                    intent,
                    fence,
                    db_now=locked.db_now,
                )
            except _GPUProofInvalid as exc:
                raise GPUEvictionCancelRuntimeSubjectError(exc.reason) from None
            if (
                intent.gpu_resource_id != gpu_resource_id
                or intent.membership_epoch != membership.membership_epoch
                or intent.owner_id != owner_id
            ):
                raise GPUEvictionCancelRuntimeSubjectError(
                    "cancel_intent_identity_changed"
                )
            source = GPUPreparedIdleEvictionRuntimeSubject(
                backend=_snapshot_gpu_mode_backend(registry),
                backend_registry_id=backend_registry_id,
                gpu_resource_id=gpu_resource_id,
                membership_epoch=membership.membership_epoch,
                budget_mb=membership.vram_budget_mb,
                eviction_priority=membership.eviction_priority,
                max_concurrency=membership.max_concurrency,
                boot_id=intent.boot_id,
                source_generation=str(intent.source_generation),
                generation=str(intent.drain_generation),
                pool_ids=pool_ids,
                control_epoch=str(intent.control_epoch),
                runtime_epoch=str(intent.runtime_epoch),
                token_expires_at=intent.drain_token_expires_at,
                require_idle=False,
                db_now=locked.db_now,
            )
            try:
                durable_reason = _eviction_terminal_durable_reason(
                    membership,
                    fence,
                    registry,
                    source,
                )
            except _GPUProofInvalid as exc:
                durable_reason = exc.reason
            if durable_reason is not None:
                raise GPUEvictionCancelRuntimeSubjectError(durable_reason)
            if (
                _eviction_cancel_source_fingerprint(source)
                != intent.subject_fingerprint
            ):
                raise GPUEvictionCancelRuntimeSubjectError(
                    "cancel_intent_source_changed"
                )
            return _prepared_eviction_cancel_subject(
                source,
                intent,
                db_now=locked.db_now,
                idempotent=True,
            )


async def _lock_gpu_cold_runtime_subject(
    db: AsyncSession,
    expected_subject: GPUColdRuntimeSubject,
    *,
    evidence_ttl: timedelta,
) -> GPUColdRuntimeSubject:
    membership = await db.scalar(
        select(GPUBackendMembership)
        .where(
            GPUBackendMembership.backend_registry_id
            == expected_subject.backend_registry_id,
            GPUBackendMembership.gpu_resource_id == expected_subject.gpu_resource_id,
            GPUBackendMembership.membership_epoch == expected_subject.membership_epoch,
            GPUBackendMembership.state == "active",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if membership is None:
        raise GPUColdRuntimeSubjectError("membership_changed")
    fence = await db.scalar(
        select(GPUBackendFence)
        .where(
            GPUBackendFence.backend_registry_id == expected_subject.backend_registry_id
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    registry = await db.scalar(
        select(MLBackendRegistry)
        .where(MLBackendRegistry.id == expected_subject.backend_registry_id)
        .execution_options(populate_existing=True)
    )
    db_now = await db.scalar(select(func.clock_timestamp()))
    if fence is None or registry is None:
        raise GPUColdRuntimeSubjectError("runtime_subject_missing")
    if (
        not isinstance(db_now, datetime)
        or db_now.tzinfo is None
        or db_now.utcoffset() is None
    ):
        raise RuntimeError("PostgreSQL returned an invalid runtime proof clock")
    try:
        current_subject = _validate_gpu_cold_runtime_subject(
            membership,
            fence,
            registry,
            db_now=db_now,
            evidence_ttl=evidence_ttl,
        )
    except _GPUProofInvalid as exc:
        raise GPUColdRuntimeSubjectError(exc.reason) from None
    if _cold_runtime_subject_identity(
        current_subject
    ) != _cold_runtime_subject_identity(expected_subject):
        raise GPUColdRuntimeSubjectError("runtime_subject_changed")
    return current_subject


async def prepare_gpu_cold_runtime_generation(
    session_factory: GPUFenceSessionFactory,
    expected_subject: GPUColdRuntimeSubject,
    *,
    token_expires_at: datetime,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPUPreparedColdRuntimeSubject:
    """Atomically reserve one cold generation and its token horizon."""

    _validate_token_expiry(token_expires_at)
    _validate_runtime_subject_evidence_ttl(evidence_ttl)
    async with session_factory() as db:
        async with db.begin():
            current_subject = await _lock_gpu_cold_runtime_subject(
                db,
                expected_subject,
                evidence_ttl=evidence_ttl,
            )
            if token_expires_at <= current_subject.db_now:
                raise GPUColdRuntimeSubjectError("token_expiry_not_in_future")
            generation = await _advance_gpu_backend_fence_in_transaction(
                db,
                expected_subject.backend_registry_id,
                "generation",
                gpu_resource_id=expected_subject.gpu_resource_id,
                membership_epoch=expected_subject.membership_epoch,
                token_expires_at=token_expires_at,
            )
    return GPUPreparedColdRuntimeSubject(
        backend_registry_id=current_subject.backend_registry_id,
        gpu_resource_id=current_subject.gpu_resource_id,
        membership_epoch=current_subject.membership_epoch,
        budget_mb=current_subject.budget_mb,
        eviction_priority=current_subject.eviction_priority,
        max_concurrency=current_subject.max_concurrency,
        boot_id=current_subject.boot_id,
        observed_generation=current_subject.observed_generation,
        generation=str(generation),
        control_epoch=current_subject.control_epoch,
        runtime_epoch=current_subject.runtime_epoch,
        token_expires_at=token_expires_at,
        db_now=current_subject.db_now,
    )


async def record_gpu_resident_runtime_token_expiry(
    session_factory: GPUFenceSessionFactory,
    expected_subject: GPUResidentRuntimeSubject,
    *,
    token_expires_at: datetime,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> datetime:
    """Revalidate one subject under ordered locks and commit its token horizon."""

    _validate_token_expiry(token_expires_at)
    async with session_factory() as db:
        async with db.begin():
            membership = await db.scalar(
                select(GPUBackendMembership)
                .where(
                    GPUBackendMembership.backend_registry_id
                    == expected_subject.backend_registry_id,
                    GPUBackendMembership.gpu_resource_id
                    == expected_subject.gpu_resource_id,
                    GPUBackendMembership.membership_epoch
                    == expected_subject.membership_epoch,
                    GPUBackendMembership.state == "active",
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if membership is None:
                raise GPUResidentRuntimeSubjectError("membership_changed")
            fence = await db.scalar(
                select(GPUBackendFence)
                .where(
                    GPUBackendFence.backend_registry_id
                    == expected_subject.backend_registry_id
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            registry = await db.scalar(
                select(MLBackendRegistry)
                .where(MLBackendRegistry.id == expected_subject.backend_registry_id)
                .execution_options(populate_existing=True)
            )
            db_now = await db.scalar(select(func.clock_timestamp()))
            if fence is None or registry is None:
                raise GPUResidentRuntimeSubjectError("runtime_subject_missing")
            if (
                not isinstance(db_now, datetime)
                or db_now.tzinfo is None
                or db_now.utcoffset() is None
            ):
                raise RuntimeError("PostgreSQL returned an invalid runtime proof clock")
            try:
                current_subject = _validate_gpu_resident_runtime_subject(
                    membership,
                    fence,
                    registry,
                    db_now=db_now,
                    evidence_ttl=evidence_ttl,
                )
            except _GPUProofInvalid as exc:
                raise GPUResidentRuntimeSubjectError(exc.reason) from None
            if _runtime_subject_identity(current_subject) != _runtime_subject_identity(
                expected_subject
            ):
                raise GPUResidentRuntimeSubjectError("runtime_subject_changed")
            if token_expires_at <= db_now:
                raise GPUResidentRuntimeSubjectError("token_expiry_not_in_future")
            return await _record_gpu_backend_token_expiry_in_transaction(
                db,
                expected_subject.backend_registry_id,
                gpu_resource_id=expected_subject.gpu_resource_id,
                membership_epoch=expected_subject.membership_epoch,
                token_expires_at=token_expires_at,
            )


async def _lock_gpu_resource_proof_domain(
    db: AsyncSession,
    resource_id: str,
    *,
    wait_for_lock: bool = True,
) -> _LockedGPUProofDomain:
    lock_function = (
        "pg_advisory_xact_lock" if wait_for_lock else "pg_try_advisory_xact_lock"
    )
    acquired = await db.scalar(
        text(
            f"SELECT {lock_function}("  # noqa: S608 - fixed internal function name
            "hashtextextended('aap:gpu-resource:' || :resource_id, 0))"
        ),
        {"resource_id": resource_id},
    )
    if not wait_for_lock and acquired is not True:
        raise _GPUProofInvalid("gpu_resource_busy")
    memberships = tuple(
        (
            await db.execute(
                select(GPUBackendMembership)
                .where(GPUBackendMembership.gpu_resource_id == resource_id)
                .order_by(GPUBackendMembership.backend_registry_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    backend_ids = [item.backend_registry_id for item in memberships]
    fences: tuple[GPUBackendFence, ...] = ()
    registries: tuple[MLBackendRegistry, ...] = ()
    if backend_ids:
        fences = tuple(
            (
                await db.execute(
                    select(GPUBackendFence)
                    .where(GPUBackendFence.backend_registry_id.in_(backend_ids))
                    .order_by(GPUBackendFence.backend_registry_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        # Registry mutation already owns its registry row before it waits for the
        # resource advisory lock.  A plain MVCC read avoids the inverse lock order;
        # the locked membership prevents that mutation from completing meanwhile.
        registries = tuple(
            (
                await db.execute(
                    select(MLBackendRegistry)
                    .where(MLBackendRegistry.id.in_(backend_ids))
                    .order_by(MLBackendRegistry.id)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
    db_now = await db.scalar(select(func.clock_timestamp()))
    if db_now is None or db_now.tzinfo is None or db_now.utcoffset() is None:
        raise RuntimeError("PostgreSQL returned an invalid proof clock")
    return _LockedGPUProofDomain(
        memberships=memberships,
        fences={item.backend_registry_id: item for item in fences},
        registries={item.id: item for item in registries},
        db_now=db_now,
    )


def _eviction_terminal_durable_reason(
    membership: GPUBackendMembership,
    fence: GPUBackendFence,
    registry: MLBackendRegistry,
    expected_subject: (
        GPUPreparedIdleEvictionRuntimeSubject | GPUPreparedEvictionCancelRuntimeSubject
    ),
) -> str | None:
    try:
        generation = _strict_nonnegative_int64(
            fence.generation_high_water,
            reason="generation_high_water_invalid",
        )
        control_epoch = _strict_nonnegative_int64(
            fence.control_epoch_high_water,
            reason="control_epoch_high_water_invalid",
        )
        runtime_epoch = _strict_nonnegative_int64(
            fence.runtime_epoch_high_water,
            reason="runtime_epoch_high_water_invalid",
        )
    except _GPUProofInvalid as exc:
        return exc.reason
    if (
        membership.state != "active"
        or membership.backend_registry_id != expected_subject.backend_registry_id
        or membership.gpu_resource_id != expected_subject.gpu_resource_id
        or membership.membership_epoch != expected_subject.membership_epoch
        or membership.vram_budget_mb != expected_subject.budget_mb
        or membership.eviction_priority != expected_subject.eviction_priority
        or membership.max_concurrency != expected_subject.max_concurrency
    ):
        return "membership_changed"
    # A concurrently prepared but never exposed generation may legitimately leave
    # a gap above this owner.  Exact Redis owner/generation and fresh health still
    # fence the active transition; only a regressed durable high-water is stale.
    if generation < int(expected_subject.generation):
        return "generation_changed"
    if control_epoch != int(expected_subject.control_epoch):
        return "control_epoch_changed"
    if runtime_epoch != int(expected_subject.runtime_epoch):
        return "runtime_epoch_changed"
    horizon = fence.token_expiry_high_water
    if (
        horizon is None
        or horizon.tzinfo is None
        or horizon.utcoffset() is None
        or horizon.astimezone(UTC) < expected_subject.token_expires_at.astimezone(UTC)
    ):
        return "token_horizon_changed"
    if (
        registry.url != expected_subject.backend.url
        or registry.auth_method != expected_subject.backend.auth_method
        or registry.auth_token != expected_subject.backend.auth_token
        or (registry.auth_revision or 0)
        != (expected_subject.backend.auth_revision or 0)
        or registry.gpu_resource_id != expected_subject.gpu_resource_id
        or registry.vram_budget_mb != expected_subject.budget_mb
        or registry.eviction_priority != expected_subject.eviction_priority
        or _registry_gpu_max_concurrency(registry.extra_params)
        != expected_subject.max_concurrency
    ):
        return "registry_claim_changed"
    return None


def _read_exact_gpu_eviction_residency(
    membership: GPUBackendMembership,
    registry: MLBackendRegistry,
    expected_subject: (
        GPUPreparedIdleEvictionRuntimeSubject | GPUPreparedEvictionCancelRuntimeSubject
    ),
    *,
    challenge: str,
    db_now: datetime,
    evidence_ttl: timedelta,
) -> _GPUProofResidency:
    if registry.state != "connected" or type(registry.health_meta) is not dict:
        raise _GPUProofInvalid("eviction_health_unavailable")
    raw_health = registry.health_meta
    probe = _parse_gpu_proof_probe(raw_health.get("gpu_arbiter_probe"))
    if probe.raw["challenge"] != challenge:
        raise _GPUProofInvalid("eviction_challenge_mismatch")
    capability_sha256 = _health_managed_lifecycle_sha256(raw_health)
    if (
        capability_sha256 is None
        or probe.managed_lifecycle_sha256 is None
        or capability_sha256 != probe.managed_lifecycle_sha256
    ):
        raise _GPUProofInvalid("managed_lifecycle_capability_mismatch")
    if (
        probe.raw["backend_registry_id"] != str(membership.backend_registry_id)
        or probe.raw["gpu_resource_id"] != membership.gpu_resource_id
        or probe.raw["membership_epoch"] != str(membership.membership_epoch)
        or probe.raw["membership_state"] != "active"
    ):
        raise _GPUProofInvalid("probe_membership_mismatch")
    if (
        registry.last_checked_at is None
        or registry.last_checked_at.tzinfo is None
        or registry.last_checked_at.utcoffset() is None
        or registry.last_checked_at.astimezone(UTC) != probe.observed_at
    ):
        raise _GPUProofInvalid("probe_registry_clock_mismatch")
    db_now_utc = db_now.astimezone(UTC)
    if probe.observed_at > db_now_utc:
        raise _GPUProofInvalid("probe_from_future")
    if db_now_utc - probe.observed_at > evidence_ttl:
        raise _GPUProofInvalid("probe_expired")

    residency = _parse_gpu_proof_residency(raw_health.get("residency"))
    identity = residency.identity
    if (
        residency.lifecycle_gate != "enforce"
        or residency.boot_id != expected_subject.boot_id
        or residency.generation != expected_subject.generation
        or residency.pool_ids != expected_subject.pool_ids
        or residency.control_epoch != expected_subject.control_epoch
        or identity is None
        or identity["backend_registry_id"] != str(expected_subject.backend_registry_id)
        or identity["gpu_resource_id"] != expected_subject.gpu_resource_id
    ):
        raise _GPUProofInvalid("eviction_residency_identity_mismatch")
    return residency


def _eviction_cancel_durable_reason(
    membership: GPUBackendMembership,
    fence: GPUBackendFence,
    registry: MLBackendRegistry,
    intent: GPUBackendCancelIntent | None,
    expected_subject: GPUPreparedEvictionCancelRuntimeSubject,
    *,
    db_now: datetime,
) -> str | None:
    durable_reason = _eviction_terminal_durable_reason(
        membership,
        fence,
        registry,
        expected_subject,
    )
    if durable_reason is not None:
        return durable_reason
    if intent is None:
        return "cancel_intent_missing"
    try:
        pool_ids = _validate_eviction_cancel_intent_durable_state(
            intent,
            fence,
            db_now=db_now,
        )
    except _GPUProofInvalid as exc:
        return exc.reason
    if (
        intent.gpu_resource_id != expected_subject.gpu_resource_id
        or intent.membership_epoch != expected_subject.membership_epoch
        or intent.boot_id != expected_subject.boot_id
        or intent.control_epoch != int(expected_subject.control_epoch)
        or intent.runtime_epoch != int(expected_subject.runtime_epoch)
        or intent.source_generation != int(expected_subject.source_generation)
        or intent.drain_generation != int(expected_subject.drain_generation)
        or intent.generation != int(expected_subject.generation)
        or intent.owner_id != expected_subject.owner_id
        or intent.operation != expected_subject.operation
        or intent.owner_hard_deadline_ms != expected_subject.owner_hard_deadline_ms
        or intent.drain_token_expires_at.astimezone(UTC)
        != expected_subject.drain_token_expires_at.astimezone(UTC)
        or intent.token_expires_at.astimezone(UTC)
        != expected_subject.token_expires_at.astimezone(UTC)
        or intent.jti != expected_subject.jti
        or pool_ids != expected_subject.pool_ids
    ):
        return "cancel_intent_changed"
    source = GPUPreparedIdleEvictionRuntimeSubject(
        backend=expected_subject.backend,
        backend_registry_id=expected_subject.backend_registry_id,
        gpu_resource_id=expected_subject.gpu_resource_id,
        membership_epoch=expected_subject.membership_epoch,
        budget_mb=expected_subject.budget_mb,
        eviction_priority=expected_subject.eviction_priority,
        max_concurrency=expected_subject.max_concurrency,
        boot_id=expected_subject.boot_id,
        source_generation=expected_subject.source_generation,
        generation=expected_subject.drain_generation,
        pool_ids=expected_subject.pool_ids,
        control_epoch=expected_subject.control_epoch,
        runtime_epoch=expected_subject.runtime_epoch,
        token_expires_at=expected_subject.drain_token_expires_at,
        require_idle=False,
        db_now=db_now,
    )
    if _eviction_cancel_source_fingerprint(source) != intent.subject_fingerprint:
        return "cancel_intent_source_changed"
    return None


def _classify_gpu_eviction_cancel(
    membership: GPUBackendMembership,
    registry: MLBackendRegistry,
    expected_subject: GPUPreparedEvictionCancelRuntimeSubject,
    *,
    challenge: str,
    db_now: datetime,
    evidence_ttl: timedelta,
) -> tuple[GPUAllocationState, str]:
    residency = _read_exact_gpu_eviction_residency(
        membership,
        registry,
        expected_subject,
        challenge=challenge,
        db_now=db_now,
        evidence_ttl=evidence_ttl,
    )
    if (
        residency.state == "resident"
        and residency.gpu_loaded is True
        and not residency.draining
        and residency.evictable
        and all(item is not None for item in residency.pool_residencies)
        and any(item is True for item in residency.pool_residencies)
    ):
        return GPUAllocationState.RESIDENT, "cancelled_resident"
    raise _GPUProofInvalid("cancel_residency_unknown")


def _gpu_draining_residency_ready(residency: _GPUProofResidency) -> bool:
    if not (
        residency.state == "draining"
        and residency.gpu_loaded is True
        and residency.draining
        and not residency.evictable
        and all(item is not None for item in residency.pool_residencies)
        and any(item is True for item in residency.pool_residencies)
    ):
        raise _GPUProofInvalid("drain_residency_not_ready")
    return (
        residency.active_requests == 0
        and residency.builders == 0
        and residency.borrowers == 0
    )


def _classify_gpu_eviction_phase(
    membership: GPUBackendMembership,
    registry: MLBackendRegistry,
    expected_subject: GPUPreparedIdleEvictionRuntimeSubject,
    *,
    phase: Literal["drain", "unload"],
    challenge: str,
    db_now: datetime,
    evidence_ttl: timedelta,
) -> tuple[GPUAllocationState, str]:
    residency = _read_exact_gpu_eviction_residency(
        membership,
        registry,
        expected_subject,
        challenge=challenge,
        db_now=db_now,
        evidence_ttl=evidence_ttl,
    )

    if phase == "drain":
        if _gpu_draining_residency_ready(residency):
            return GPUAllocationState.UNLOADING, "ready_to_unload"
        raise _GPUProofInvalid("eviction_residency_identity_mismatch")
    if phase == "unload":
        if (
            residency.state == "unloaded"
            and residency.gpu_loaded is False
            and not residency.draining
            and not residency.evictable
            and residency.active_requests == 0
            and residency.builders == 0
            and residency.borrowers == 0
            and all(item is False for item in residency.pool_residencies)
        ):
            return GPUAllocationState.UNLOADED, "unloaded"
        raise _GPUProofInvalid("unload_residency_unknown")
    raise ValueError("phase must be drain or unload")


async def read_gpu_eviction_drain_health(
    db: AsyncSession,
    expected_subject: GPUPreparedIdleEvictionRuntimeSubject,
    *,
    challenge: str,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPUEvictionDrainHealth:
    """Classify one fresh draining health receipt without mutating durable state."""

    if (
        not isinstance(challenge, str)
        or _GPU_HEALTH_CHALLENGE_RE.fullmatch(challenge) is None
    ):
        raise ValueError("challenge must be 64 lowercase hexadecimal characters")
    _validate_runtime_subject_evidence_ttl(evidence_ttl)
    row = (
        await db.execute(
            select(
                GPUBackendMembership,
                GPUBackendFence,
                MLBackendRegistry,
                func.clock_timestamp(),
            )
            .join(
                GPUBackendFence,
                GPUBackendFence.backend_registry_id
                == GPUBackendMembership.backend_registry_id,
            )
            .join(
                MLBackendRegistry,
                MLBackendRegistry.id == GPUBackendMembership.backend_registry_id,
            )
            .where(
                GPUBackendMembership.backend_registry_id
                == expected_subject.backend_registry_id,
                GPUBackendMembership.gpu_resource_id
                == expected_subject.gpu_resource_id,
                GPUBackendMembership.state == "active",
            )
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        return GPUEvictionDrainHealth(
            status="uncertain",
            reason="runtime_subject_missing",
        )
    membership, fence, registry, db_now = row
    if (
        not isinstance(db_now, datetime)
        or db_now.tzinfo is None
        or db_now.utcoffset() is None
    ):
        raise RuntimeError("PostgreSQL returned an invalid runtime proof clock")
    try:
        durable_reason = _eviction_terminal_durable_reason(
            membership,
            fence,
            registry,
            expected_subject,
        )
    except _GPUProofInvalid as exc:
        durable_reason = exc.reason
    if durable_reason is not None:
        return GPUEvictionDrainHealth(
            status="uncertain",
            reason=durable_reason,
        )
    try:
        residency = _read_exact_gpu_eviction_residency(
            membership,
            registry,
            expected_subject,
            challenge=challenge,
            db_now=db_now,
            evidence_ttl=evidence_ttl,
        )
        ready = _gpu_draining_residency_ready(residency)
    except _GPUProofInvalid as exc:
        return GPUEvictionDrainHealth(
            status="uncertain",
            reason=exc.reason,
        )
    return GPUEvictionDrainHealth(
        status="ready_to_unload" if ready else "draining_busy",
        reason="ready_to_unload" if ready else "draining_busy",
        active_requests=residency.active_requests,
        builders=residency.builders,
        borrowers=residency.borrowers,
    )


async def commit_gpu_eviction_phase_from_health(
    session_factory: GPUFenceSessionFactory,
    store: GPUArbiterStore,
    expected_subject: GPUPreparedIdleEvictionRuntimeSubject,
    *,
    phase: Literal["drain", "unload"],
    challenge: str | None,
    owner_id: str,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPUEvictionCommitResult:
    """Commit a drain/unload phase; untrusted health conservatively becomes Unknown."""

    if phase not in {"drain", "unload"}:
        raise ValueError("phase must be drain or unload")
    if challenge is not None and _GPU_HEALTH_CHALLENGE_RE.fullmatch(challenge) is None:
        raise ValueError("challenge must be 64 lowercase hexadecimal characters")
    _validate_runtime_subject_evidence_ttl(evidence_ttl)
    async with session_factory() as db:
        async with db.begin():
            locked = await _lock_gpu_resource_proof_domain(
                db,
                expected_subject.gpu_resource_id,
            )
            membership = next(
                (
                    item
                    for item in locked.memberships
                    if item.backend_registry_id == expected_subject.backend_registry_id
                    and item.membership_epoch == expected_subject.membership_epoch
                    and item.state == "active"
                ),
                None,
            )
            fence = locked.fences.get(expected_subject.backend_registry_id)
            registry = locked.registries.get(expected_subject.backend_registry_id)
            if membership is None or fence is None or registry is None:
                return GPUEvictionCommitResult(
                    status="stale",
                    state=GPUAllocationState.UNKNOWN,
                    reason="runtime_subject_missing",
                )
            durable_reason = _eviction_terminal_durable_reason(
                membership,
                fence,
                registry,
                expected_subject,
            )
            if durable_reason is not None:
                return GPUEvictionCommitResult(
                    status="stale",
                    state=GPUAllocationState.UNKNOWN,
                    reason=durable_reason,
                )
            try:
                if challenge is None:
                    raise _GPUProofInvalid("eviction_response_uncertain")
                target_state, reason = _classify_gpu_eviction_phase(
                    membership,
                    registry,
                    expected_subject,
                    phase=phase,
                    challenge=challenge,
                    db_now=locked.db_now,
                    evidence_ttl=evidence_ttl,
                )
            except _GPUProofInvalid as exc:
                target_state = GPUAllocationState.UNKNOWN
                reason = exc.reason
            expected_state = (
                GPUAllocationState.DRAINING
                if phase == "drain"
                else GPUAllocationState.UNLOADING
            )
            transition_kwargs = {
                "backend_id": str(expected_subject.backend_registry_id),
                "expected_state": expected_state,
                "expected_generation": expected_subject.generation,
                "target_state": target_state,
                "transition_owner_id": owner_id,
            }
            try:
                transition = await store.transition_eviction_allocation(
                    expected_subject.gpu_resource_id,
                    **transition_kwargs,
                )
            except Exception:  # noqa: BLE001 - exact idempotent response-loss retry
                transition = await store.transition_eviction_allocation(
                    expected_subject.gpu_resource_id,
                    **transition_kwargs,
                )
            if transition.status != "transitioned":
                return GPUEvictionCommitResult(
                    status="rejected",
                    state=target_state,
                    reason=f"redis_{transition.status}",
                )
            return GPUEvictionCommitResult(
                status="finalized",
                state=target_state,
                reason=reason,
                idempotent=transition.idempotent,
            )


async def commit_gpu_eviction_cancel_from_health(
    session_factory: GPUFenceSessionFactory,
    store: GPUArbiterStore,
    expected_subject: GPUPreparedEvictionCancelRuntimeSubject,
    *,
    ack_confirmed: bool,
    challenge: str | None,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPUEvictionCommitResult:
    """Commit one passive cancel proof; uncertainty can only tighten to Unknown.

    This helper deliberately does not send RESUME or choose between cancel and
    unload.  The authority must install a Redis branch fence before either active
    branch is wired.
    """

    if not isinstance(ack_confirmed, bool):
        raise ValueError("ack_confirmed must be a boolean")
    if challenge is not None and _GPU_HEALTH_CHALLENGE_RE.fullmatch(challenge) is None:
        raise ValueError("challenge must be 64 lowercase hexadecimal characters")
    _validate_runtime_subject_evidence_ttl(evidence_ttl)
    async with session_factory() as db:
        async with db.begin():
            locked = await _lock_gpu_resource_proof_domain(
                db,
                expected_subject.gpu_resource_id,
            )
            membership = next(
                (
                    item
                    for item in locked.memberships
                    if item.backend_registry_id == expected_subject.backend_registry_id
                    and item.membership_epoch == expected_subject.membership_epoch
                    and item.state == "active"
                ),
                None,
            )
            fence = locked.fences.get(expected_subject.backend_registry_id)
            registry = locked.registries.get(expected_subject.backend_registry_id)
            intent = await db.scalar(
                select(GPUBackendCancelIntent)
                .where(
                    GPUBackendCancelIntent.backend_registry_id
                    == expected_subject.backend_registry_id
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if membership is None or fence is None or registry is None:
                return GPUEvictionCommitResult(
                    status="stale",
                    state=GPUAllocationState.UNKNOWN,
                    reason="runtime_subject_missing",
                )
            durable_reason = _eviction_cancel_durable_reason(
                membership,
                fence,
                registry,
                intent,
                expected_subject,
                db_now=locked.db_now,
            )
            if durable_reason is not None:
                return GPUEvictionCommitResult(
                    status="stale",
                    state=GPUAllocationState.UNKNOWN,
                    reason=durable_reason,
                )
            try:
                if not ack_confirmed:
                    raise _GPUProofInvalid("cancel_ack_uncertain")
                if challenge is None:
                    raise _GPUProofInvalid("cancel_health_uncertain")
                target_state, reason = _classify_gpu_eviction_cancel(
                    membership,
                    registry,
                    expected_subject,
                    challenge=challenge,
                    db_now=locked.db_now,
                    evidence_ttl=evidence_ttl,
                )
            except _GPUProofInvalid as exc:
                target_state = GPUAllocationState.UNKNOWN
                reason = exc.reason
            transition_kwargs = {
                "backend_id": str(expected_subject.backend_registry_id),
                "expected_state": GPUAllocationState.DRAINING,
                "expected_generation": expected_subject.drain_generation,
                "target_state": target_state,
                "transition_owner_id": expected_subject.owner_id,
                "next_generation": (
                    expected_subject.generation
                    if target_state is GPUAllocationState.RESIDENT
                    else None
                ),
            }
            try:
                transition = await store.transition_eviction_allocation(
                    expected_subject.gpu_resource_id,
                    **transition_kwargs,
                )
            except Exception:  # noqa: BLE001 - exact idempotent response-loss retry
                transition = await store.transition_eviction_allocation(
                    expected_subject.gpu_resource_id,
                    **transition_kwargs,
                )
            if transition.status != "transitioned":
                return GPUEvictionCommitResult(
                    status="rejected",
                    state=target_state,
                    reason=f"redis_{transition.status}",
                )
            return GPUEvictionCommitResult(
                status="finalized",
                state=target_state,
                reason=reason,
                idempotent=transition.idempotent,
            )


def _cold_terminal_durable_reason(
    membership: GPUBackendMembership,
    fence: GPUBackendFence,
    registry: MLBackendRegistry,
    expected_subject: GPUPreparedColdRuntimeSubject,
) -> str | None:
    try:
        generation = _strict_nonnegative_int64(
            fence.generation_high_water,
            reason="generation_high_water_invalid",
        )
        control_epoch = _strict_nonnegative_int64(
            fence.control_epoch_high_water,
            reason="control_epoch_high_water_invalid",
        )
        runtime_epoch = _strict_nonnegative_int64(
            fence.runtime_epoch_high_water,
            reason="runtime_epoch_high_water_invalid",
        )
    except _GPUProofInvalid as exc:
        return exc.reason
    if (
        membership.state != "active"
        or membership.backend_registry_id != expected_subject.backend_registry_id
        or membership.gpu_resource_id != expected_subject.gpu_resource_id
        or membership.membership_epoch != expected_subject.membership_epoch
        or membership.vram_budget_mb != expected_subject.budget_mb
        or membership.eviction_priority != expected_subject.eviction_priority
        or membership.max_concurrency != expected_subject.max_concurrency
    ):
        return "membership_changed"
    if generation != int(expected_subject.generation):
        return "generation_changed"
    if control_epoch != int(expected_subject.control_epoch):
        return "control_epoch_changed"
    if runtime_epoch != int(expected_subject.runtime_epoch):
        return "runtime_epoch_changed"
    horizon = fence.token_expiry_high_water
    if (
        horizon is None
        or horizon.tzinfo is None
        or horizon.utcoffset() is None
        or horizon.astimezone(UTC) < expected_subject.token_expires_at.astimezone(UTC)
    ):
        return "token_horizon_changed"
    if (
        registry.gpu_resource_id != expected_subject.gpu_resource_id
        or registry.vram_budget_mb != expected_subject.budget_mb
        or registry.eviction_priority != expected_subject.eviction_priority
        or _registry_gpu_max_concurrency(registry.extra_params)
        != expected_subject.max_concurrency
    ):
        return "registry_claim_changed"
    return None


def _cold_terminal_cpu_fallback(raw_health: dict[str, Any]) -> bool:
    compute = raw_health.get("compute")
    if type(compute) is not dict or compute.get("cpu_fallback_supported") is not True:
        return False
    effective_device = compute.get("effective_device")
    effective_provider = compute.get("effective_provider")
    return (
        type(effective_device) is str and effective_device.strip().lower() == "cpu"
    ) or (
        type(effective_provider) is str
        and effective_provider.strip().lower() == "cpuexecutionprovider"
    )


def _classify_gpu_cold_terminal(
    membership: GPUBackendMembership,
    registry: MLBackendRegistry,
    expected_subject: GPUPreparedColdRuntimeSubject,
    *,
    challenge: str,
    db_now: datetime,
    evidence_ttl: timedelta,
) -> tuple[GPUAllocationState, str]:
    if registry.state != "connected" or type(registry.health_meta) is not dict:
        raise _GPUProofInvalid("terminal_health_unavailable")
    raw_health = registry.health_meta
    probe = _parse_gpu_proof_probe(raw_health.get("gpu_arbiter_probe"))
    if probe.raw["challenge"] != challenge:
        raise _GPUProofInvalid("terminal_challenge_mismatch")
    capability_sha256 = _health_managed_lifecycle_sha256(raw_health)
    if (
        capability_sha256 is None
        or probe.managed_lifecycle_sha256 is None
        or capability_sha256 != probe.managed_lifecycle_sha256
    ):
        raise _GPUProofInvalid("managed_lifecycle_capability_mismatch")
    if (
        probe.raw["backend_registry_id"] != str(membership.backend_registry_id)
        or probe.raw["gpu_resource_id"] != membership.gpu_resource_id
        or probe.raw["membership_epoch"] != str(membership.membership_epoch)
        or probe.raw["membership_state"] != "active"
    ):
        raise _GPUProofInvalid("probe_membership_mismatch")
    if (
        registry.last_checked_at is None
        or registry.last_checked_at.tzinfo is None
        or registry.last_checked_at.utcoffset() is None
        or registry.last_checked_at.astimezone(UTC) != probe.observed_at
    ):
        raise _GPUProofInvalid("probe_registry_clock_mismatch")
    db_now_utc = db_now.astimezone(UTC)
    if probe.observed_at > db_now_utc:
        raise _GPUProofInvalid("probe_from_future")
    if db_now_utc - probe.observed_at > evidence_ttl:
        raise _GPUProofInvalid("probe_expired")

    residency = _parse_gpu_proof_residency(raw_health.get("residency"))
    identity = residency.identity
    if (
        residency.lifecycle_gate != "enforce"
        or residency.boot_id != expected_subject.boot_id
        or residency.control_epoch != expected_subject.control_epoch
        or identity is None
        or identity["backend_registry_id"] != str(expected_subject.backend_registry_id)
        or identity["gpu_resource_id"] != expected_subject.gpu_resource_id
        or residency.active_requests != 0
        or residency.builders != 0
        or residency.borrowers != 0
        or residency.draining
    ):
        raise _GPUProofInvalid("terminal_residency_identity_mismatch")

    if (
        residency.state == "resident"
        and residency.gpu_loaded is True
        and residency.evictable
        and residency.generation == expected_subject.generation
        and all(item is not None for item in residency.pool_residencies)
        and any(item is True for item in residency.pool_residencies)
    ):
        return GPUAllocationState.RESIDENT, "resident"
    if (
        residency.state == "resident"
        and residency.gpu_loaded is False
        and not residency.evictable
        and residency.generation == expected_subject.generation
        and all(item is False for item in residency.pool_residencies)
        and _cold_terminal_cpu_fallback(raw_health)
    ):
        return GPUAllocationState.CPU_FALLBACK, "cpu_fallback"
    if (
        residency.state == "unloaded"
        and residency.gpu_loaded is False
        and not residency.evictable
        and all(item is False for item in residency.pool_residencies)
        and (
            residency.generation is None
            or int(residency.generation) <= int(expected_subject.generation)
        )
    ):
        return GPUAllocationState.UNLOADED, "unloaded"
    raise _GPUProofInvalid("terminal_residency_unknown")


async def commit_gpu_cold_terminal_from_health(
    session_factory: GPUFenceSessionFactory,
    store: GPUArbiterStore,
    expected_subject: GPUPreparedColdRuntimeSubject,
    *,
    challenge: str | None,
    lease_id: str,
    owner_id: str,
    resident_cooldown_ms: int,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPUColdTerminalCommitResult:
    """Commit one exposed cold terminal; no response proof forces Unknown."""

    if challenge is not None and _GPU_HEALTH_CHALLENGE_RE.fullmatch(challenge) is None:
        raise ValueError("challenge must be 64 lowercase hexadecimal characters")
    if (
        isinstance(resident_cooldown_ms, bool)
        or not isinstance(resident_cooldown_ms, int)
        or resident_cooldown_ms <= 0
        or resident_cooldown_ms > 3_600_000
    ):
        raise ValueError("resident_cooldown_ms must be between 1 and 3600000")
    _validate_runtime_subject_evidence_ttl(evidence_ttl)
    async with session_factory() as db:
        async with db.begin():
            locked = await _lock_gpu_resource_proof_domain(
                db,
                expected_subject.gpu_resource_id,
            )
            membership = next(
                (
                    item
                    for item in locked.memberships
                    if item.backend_registry_id == expected_subject.backend_registry_id
                    and item.membership_epoch == expected_subject.membership_epoch
                    and item.state == "active"
                ),
                None,
            )
            fence = locked.fences.get(expected_subject.backend_registry_id)
            registry = locked.registries.get(expected_subject.backend_registry_id)
            if membership is None or fence is None or registry is None:
                return GPUColdTerminalCommitResult(
                    status="stale",
                    state=GPUAllocationState.UNKNOWN,
                    reason="runtime_subject_missing",
                )
            durable_reason = _cold_terminal_durable_reason(
                membership,
                fence,
                registry,
                expected_subject,
            )
            if durable_reason is not None:
                return GPUColdTerminalCommitResult(
                    status="stale",
                    state=GPUAllocationState.UNKNOWN,
                    reason=durable_reason,
                )
            try:
                if challenge is None:
                    raise _GPUProofInvalid("terminal_response_uncertain")
                target_state, reason = _classify_gpu_cold_terminal(
                    membership,
                    registry,
                    expected_subject,
                    challenge=challenge,
                    db_now=locked.db_now,
                    evidence_ttl=evidence_ttl,
                )
            except _GPUProofInvalid as exc:
                target_state = GPUAllocationState.UNKNOWN
                reason = exc.reason
            finalize_kwargs = {
                "backend_id": str(expected_subject.backend_registry_id),
                "expected_generation": expected_subject.generation,
                "request_lease_id": lease_id,
                "request_owner_id": owner_id,
                "target_state": target_state,
                "target_evictable": target_state is GPUAllocationState.RESIDENT,
                "resident_cooldown_ms": (
                    resident_cooldown_ms
                    if target_state is GPUAllocationState.RESIDENT
                    else 0
                ),
            }
            try:
                transition = await store.finalize_cold_allocation(
                    expected_subject.gpu_resource_id,
                    **finalize_kwargs,
                )
            except Exception:  # noqa: BLE001 - exact idempotent response-loss retry
                transition = await store.finalize_cold_allocation(
                    expected_subject.gpu_resource_id,
                    **finalize_kwargs,
                )
            if transition.status != "transitioned":
                return GPUColdTerminalCommitResult(
                    status="rejected",
                    state=target_state,
                    reason=f"redis_{transition.status}",
                )
            return GPUColdTerminalCommitResult(
                status="finalized",
                state=target_state,
                reason=reason,
                idempotent=transition.idempotent,
            )


def _snapshot_gpu_mode_backend(registry: MLBackendRegistry) -> MLBackendRegistry:
    """Detach only endpoint fields needed by the post-commit control client."""

    return MLBackendRegistry(
        id=registry.id,
        name=registry.name,
        url=registry.url,
        state=registry.state,
        auth_method=registry.auth_method,
        auth_token=registry.auth_token,
        auth_revision=registry.auth_revision or 0,
        extra_params=dict(registry.extra_params),
        gpu_resource_id=registry.gpu_resource_id,
        vram_budget_mb=registry.vram_budget_mb,
        eviction_priority=registry.eviction_priority,
    )


def _validate_stable_legacy_residency(residency: _GPUProofResidency) -> None:
    idle = (
        residency.active_requests == 0
        and residency.builders == 0
        and residency.borrowers == 0
        and not residency.draining
    )
    pools_complete = all(item is not None for item in residency.pool_residencies)
    if (
        residency.lifecycle_gate != "legacy"
        or residency.generation is not None
        or residency.evictable
        or not idle
        or residency.state not in {"unloaded", "resident"}
        or residency.gpu_loaded is None
        or not pools_complete
    ):
        raise _GPUProofInvalid("legacy_residency_not_stably_idle")
    if residency.gpu_loaded is True and (
        residency.state != "resident"
        or not any(item is True for item in residency.pool_residencies)
    ):
        raise _GPUProofInvalid("legacy_residency_loaded_inconsistent")


def _optional_datetime_document(value: datetime | None) -> Any:
    if value is None:
        return None
    try:
        return _canonical_proof_timestamp(value)
    except _GPUProofInvalid:
        return {"invalid_naive_timestamp": value.isoformat()}


def _gpu_domain_members(
    memberships: Iterable[GPUBackendMembership],
) -> tuple[GPUBackendDomainMember, ...]:
    return tuple(
        GPUBackendDomainMember(
            backend_id=str(item.backend_registry_id),
            membership_epoch=item.membership_epoch,
            state=item.state,  # type: ignore[arg-type]
        )
        for item in memberships
    )
