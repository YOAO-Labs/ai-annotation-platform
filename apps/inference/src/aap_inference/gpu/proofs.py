"""GPU proof schema, runtime subjects, and terminal commits.

Moved verbatim from the legacy aggregate ``gpu_arbiter.py``. This module owns the
canonical proof parser, residency parser, runtime subject dataclasses and errors,
generation preparation, token horizon, drain health classification, and the
cold / eviction / eviction-cancel terminal commits. It also holds the shared
proof-domain primitives (_lock_gpu_resource_proof_domain, _optional_datetime_document,
_gpu_domain_members) consumed by control_preparation, reconciliation and retirement.

Depends on contracts, policy, the fixed GPU Native authority, fences and ledger.
Must not depend on ml_client or any high-level orchestration module.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, fields as dataclass_fields
from datetime import UTC, datetime, timedelta
import re
from types import UnionType
import uuid
from typing import Any, Literal, TypeVar, Union, get_args, get_origin, get_type_hints

import structlog
from aap_protocol_v2.lifecycle import (
    managed_lifecycle_capability_sha256,
    validate_canonical_positive_int64,
)

from aap_inference.gpu.native import GPUAuthorityData, GPUAuthorityDataProvider
from aap_inference.gpu.ledger import (
    GPU_EVICTION_OPERATION,
    GPUAllocation,
    GPUAllocationState,
    GPUArbiterStore,
    GPUBackendDomainMember,
    normalize_gpu_backend_max_concurrency,
)
from aap_inference.gpu.policy import (
    _CANONICAL_POSITIVE_INT64_RE,
    _HEALTH_EVIDENCE_MAX_AGE,
    _MAX_POSITIVE_INT64,
)
from aap_inference.gpu.resource import validate_gpu_resource_id
from aap_inference.gpu.fences import _validate_token_expiry
from aap_inference_contract.gpu import GPUAuthorityUnavailable


logger = structlog.get_logger(__name__)

_PROOF_RESET_MAX_WINDOW = timedelta(minutes=5)
_GPU_HEALTH_CHALLENGE_RE = re.compile(r"[0-9a-f]{64}\Z")
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
_GPU_EVICTION_PRIORITY_MIN = -(2**31)
_GPU_EVICTION_PRIORITY_MAX = 2**31 - 1
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
class GPUProofMembership:
    backend_registry_id: uuid.UUID
    gpu_resource_id: str
    membership_epoch: int
    state: Literal["pending", "active", "retiring"]
    vram_budget_mb: int
    eviction_priority: int
    max_concurrency: int
    runtime_epoch_baseline: int
    retired_at: datetime | None
    retire_reason: str | None
    retired_generation_high_water: int | None
    retired_control_epoch_high_water: int | None
    retired_runtime_epoch_high_water: int | None
    retired_token_expiry_high_water: datetime | None


@dataclass(frozen=True)
class GPUProofFence:
    backend_registry_id: uuid.UUID
    generation_high_water: int
    control_epoch_high_water: int
    runtime_epoch_high_water: int
    token_expiry_high_water: datetime | None


@dataclass(frozen=True)
class GPUProofRegistry:
    id: uuid.UUID
    state: str
    gpu_resource_id: str | None
    vram_budget_mb: int | None
    eviction_priority: int | None
    extra_params: object
    health_meta: object
    last_checked_at: datetime | None


def _native_uuid(value: object) -> uuid.UUID:
    if type(value) is not str:
        raise ValueError
    parsed = uuid.UUID(value)
    if str(parsed) != value:
        raise ValueError
    return parsed


def _native_datetime(value: object, *, optional: bool = False) -> datetime | None:
    if optional and value is None:
        return None
    if type(value) is not str:
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    return parsed.astimezone(UTC)


def _native_int(value: object, *, reason: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_POSITIVE_INT64:
        raise _GPUProofInvalid(reason)
    return value


def _native_priority(value: object, *, reason: str) -> int:
    if (
        type(value) is not int
        or not _GPU_EVICTION_PRIORITY_MIN <= value <= _GPU_EVICTION_PRIORITY_MAX
    ):
        raise _GPUProofInvalid(reason)
    return value


def _native_optional_int(value: object, *, reason: str) -> int | None:
    if value is None:
        return None
    return _native_int(value, reason=reason)


def _parse_proof_membership(value: object) -> GPUProofMembership:
    expected = {
        "backend_registry_id",
        "gpu_resource_id",
        "membership_epoch",
        "state",
        "vram_budget_mb",
        "eviction_priority",
        "max_concurrency",
        "runtime_epoch_baseline",
        "retired_at",
        "retire_reason",
        "retired_generation_high_water",
        "retired_control_epoch_high_water",
        "retired_runtime_epoch_high_water",
        "retired_token_expiry_high_water",
    }
    if type(value) is not dict or set(value) != expected:
        raise GPUAuthorityUnavailable()
    try:
        state = value["state"]
        if state not in {"pending", "active", "retiring"}:
            raise ValueError
        if type(value["gpu_resource_id"]) is not str:
            raise ValueError
        retire_reason = value["retire_reason"]
        if retire_reason is not None and (
            type(retire_reason) is not str or len(retire_reason) > 32
        ):
            raise ValueError
        return GPUProofMembership(
            backend_registry_id=_native_uuid(value["backend_registry_id"]),
            gpu_resource_id=value["gpu_resource_id"],
            membership_epoch=_native_int(
                value["membership_epoch"],
                reason="membership_epoch_invalid",
            ),
            state=state,
            vram_budget_mb=_native_int(
                value["vram_budget_mb"],
                reason="membership_budget_invalid",
            ),
            eviction_priority=_native_priority(
                value["eviction_priority"],
                reason="membership_priority_invalid",
            ),
            max_concurrency=_native_int(
                value["max_concurrency"],
                reason="membership_concurrency_invalid",
            ),
            runtime_epoch_baseline=_native_int(
                value["runtime_epoch_baseline"],
                reason="runtime_epoch_baseline_invalid",
            ),
            retired_at=_native_datetime(value["retired_at"], optional=True),
            retire_reason=retire_reason,
            retired_generation_high_water=_native_optional_int(
                value["retired_generation_high_water"],
                reason="retired_generation_high_water_invalid",
            ),
            retired_control_epoch_high_water=_native_optional_int(
                value["retired_control_epoch_high_water"],
                reason="retired_control_epoch_high_water_invalid",
            ),
            retired_runtime_epoch_high_water=_native_optional_int(
                value["retired_runtime_epoch_high_water"],
                reason="retired_runtime_epoch_high_water_invalid",
            ),
            retired_token_expiry_high_water=_native_datetime(
                value["retired_token_expiry_high_water"],
                optional=True,
            ),
        )
    except GPUAuthorityUnavailable:
        raise
    except (TypeError, ValueError, KeyError):
        raise GPUAuthorityUnavailable() from None


def _parse_proof_fence(value: object) -> GPUProofFence:
    expected = {
        "backend_registry_id",
        "generation_high_water",
        "control_epoch_high_water",
        "runtime_epoch_high_water",
        "token_expires_at",
    }
    if type(value) is not dict or set(value) != expected:
        raise GPUAuthorityUnavailable()
    try:
        return GPUProofFence(
            backend_registry_id=_native_uuid(value["backend_registry_id"]),
            generation_high_water=_native_int(
                value["generation_high_water"],
                reason="generation_high_water_invalid",
            ),
            control_epoch_high_water=_native_int(
                value["control_epoch_high_water"],
                reason="control_epoch_high_water_invalid",
            ),
            runtime_epoch_high_water=_native_int(
                value["runtime_epoch_high_water"],
                reason="runtime_epoch_high_water_invalid",
            ),
            token_expiry_high_water=_native_datetime(
                value["token_expires_at"],
                optional=True,
            ),
        )
    except GPUAuthorityUnavailable:
        raise
    except (TypeError, ValueError, KeyError):
        raise GPUAuthorityUnavailable() from None


def _parse_proof_registry(value: object) -> GPUProofRegistry:
    expected = {
        "id",
        "gpu_resource_id",
        "vram_budget_mb",
        "eviction_priority",
        "extra_params",
        "state",
        "last_checked_at",
        "health_meta",
    }
    if type(value) is not dict or set(value) != expected:
        raise GPUAuthorityUnavailable()
    try:
        if type(value["state"]) is not str or (
            value["gpu_resource_id"] is not None
            and type(value["gpu_resource_id"]) is not str
        ):
            raise ValueError
        return GPUProofRegistry(
            id=_native_uuid(value["id"]),
            state=value["state"],
            gpu_resource_id=value["gpu_resource_id"],
            vram_budget_mb=(
                None
                if value["vram_budget_mb"] is None
                else _native_int(
                    value["vram_budget_mb"],
                    reason="registry_budget_invalid",
                )
            ),
            eviction_priority=(
                None
                if value["eviction_priority"] is None
                else _native_priority(
                    value["eviction_priority"],
                    reason="registry_priority_invalid",
                )
            ),
            extra_params=value["extra_params"],
            health_meta=value["health_meta"],
            last_checked_at=_native_datetime(
                value["last_checked_at"],
                optional=True,
            ),
        )
    except GPUAuthorityUnavailable:
        raise
    except (TypeError, ValueError, KeyError):
        raise GPUAuthorityUnavailable() from None


def _parse_proof_domain_row_lists(
    value: object,
) -> tuple[
    tuple[GPUProofMembership, ...],
    dict[uuid.UUID, GPUProofFence],
    dict[uuid.UUID, GPUProofRegistry],
    datetime,
]:
    if type(value) is not dict:
        raise GPUAuthorityUnavailable()
    memberships_value = value.get("memberships")
    fences_value = value.get("fences")
    registries_value = value.get("registries")
    if (
        type(memberships_value) is not list
        or type(fences_value) is not list
        or type(registries_value) is not list
    ):
        raise GPUAuthorityUnavailable()
    try:
        memberships = tuple(_parse_proof_membership(item) for item in memberships_value)
        fences = tuple(_parse_proof_fence(item) for item in fences_value)
        registries = tuple(_parse_proof_registry(item) for item in registries_value)
        membership_ids = tuple(item.backend_registry_id for item in memberships)
        fence_ids = tuple(item.backend_registry_id for item in fences)
        registry_ids = tuple(item.id for item in registries)
        if (
            membership_ids != tuple(sorted(set(membership_ids)))
            or fence_ids != tuple(sorted(set(fence_ids)))
            or registry_ids != tuple(sorted(set(registry_ids)))
        ):
            raise ValueError
        db_now = _native_datetime(value.get("db_now"))
        if db_now is None:
            raise ValueError
    except GPUAuthorityUnavailable:
        raise
    except (TypeError, ValueError, KeyError):
        raise GPUAuthorityUnavailable() from None
    return (
        memberships,
        {item.backend_registry_id: item for item in fences},
        {item.id: item for item in registries},
        db_now,
    )


def parse_gpu_proof_domain(result: object) -> _LockedGPUProofDomain:
    """Decode a closed Native proof-domain projection without ORM rows."""

    if type(result) is not dict or set(result) != {
        "status",
        "db_now",
        "memberships",
        "fences",
        "registries",
    }:
        raise GPUAuthorityUnavailable()
    if result["status"] not in {"ok", "matched", "domain_changed"}:
        raise GPUAuthorityUnavailable()
    memberships, fences, registries, db_now = _parse_proof_domain_row_lists(result)
    return _LockedGPUProofDomain(memberships, fences, registries, db_now)


@dataclass(frozen=True)
class _GPUProofSubjectFacts:
    membership: GPUProofMembership
    fence: GPUProofFence
    registry: GPUProofRegistry
    db_now: datetime
    subject: dict[str, object]


def _parse_native_proof_subject(result: object) -> _GPUProofSubjectFacts:
    if type(result) is not dict:
        raise GPUAuthorityUnavailable()
    status = result.get("status")
    if status == "blocked":
        if set(result) != {"status", "reason"}:
            raise GPUAuthorityUnavailable()
        reason = result["reason"]
        if type(reason) is not str or not reason:
            raise GPUAuthorityUnavailable()
        raise _GPUProofInvalid(reason)
    if status != "ready" or set(result) != {"status", "subject", "proof"}:
        raise GPUAuthorityUnavailable()
    subject = result["subject"]
    proof = result["proof"]
    if (
        type(subject) is not dict
        or type(proof) is not dict
        or set(proof) != {"membership", "fence", "registry", "db_now"}
    ):
        raise GPUAuthorityUnavailable()
    try:
        db_now = _native_datetime(proof["db_now"])
        if db_now is None:
            raise ValueError
        return _GPUProofSubjectFacts(
            membership=_parse_proof_membership(proof["membership"]),
            fence=_parse_proof_fence(proof["fence"]),
            registry=_parse_proof_registry(proof["registry"]),
            db_now=db_now,
            subject=subject,
        )
    except GPUAuthorityUnavailable:
        raise
    except (TypeError, ValueError, KeyError):
        raise GPUAuthorityUnavailable() from None


def _parse_native_proof_domain_subject(result: object) -> _GPUProofSubjectFacts:
    if type(result) is not dict:
        raise GPUAuthorityUnavailable()
    if result.get("status") == "blocked":
        if set(result) != {"status", "reason"}:
            raise GPUAuthorityUnavailable()
        reason = result["reason"]
        if type(reason) is not str or not reason:
            raise GPUAuthorityUnavailable()
        raise _GPUProofInvalid(reason)
    if result.get("status") != "ready" or set(result) != {"status", "subject"}:
        raise GPUAuthorityUnavailable()
    return _parse_native_proof_subject(
        {"status": "ready", "subject": {}, "proof": result["subject"]}
    )


def _native_subject_value_matches(expected: object, value: object) -> bool:
    if isinstance(expected, uuid.UUID):
        try:
            return _native_uuid(value) == expected
        except (TypeError, ValueError):
            return False
    if isinstance(expected, datetime):
        try:
            return _native_datetime(value) == expected.astimezone(UTC)
        except (TypeError, ValueError):
            return False
    if isinstance(expected, tuple):
        return type(value) is list and tuple(value) == expected
    if expected is None:
        return value is None
    if type(expected) is bool:
        return type(value) is bool and value is expected
    if type(expected) is int:
        return type(value) is int and value == expected
    if type(expected) is str:
        return type(value) is str and value == expected
    return value == expected


def _validate_native_subject_projection(
    facts: _GPUProofSubjectFacts,
    expected_subject: object,
) -> None:
    fields = dataclass_fields(expected_subject)
    if set(facts.subject) != {field.name for field in fields}:
        raise GPUAuthorityUnavailable()
    for field in fields:
        expected = getattr(expected_subject, field.name)
        if not _native_subject_value_matches(expected, facts.subject[field.name]):
            raise GPUAuthorityUnavailable()


_NativePreparedT = TypeVar("_NativePreparedT")


def _native_input_value(value: object) -> object:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return _canonical_proof_timestamp(value)
    if isinstance(value, tuple):
        return [_native_input_value(item) for item in value]
    return value


def _native_subject_input(subject: object) -> dict[str, object]:
    return {
        item.name: _native_input_value(getattr(subject, item.name))
        for item in dataclass_fields(subject)
    }


def _decode_native_subject_field(
    annotation: object,
    value: object,
    *,
    field_name: str,
) -> object:
    if value is None:
        if type(None) in get_args(annotation):
            return None
        raise ValueError
    origin = get_origin(annotation)
    if origin is UnionType or origin is Union:
        args = get_args(annotation)
        if len(args) != 2 or args[1] is not type(None):
            raise ValueError
        annotation = args[0]
        origin = get_origin(annotation)
    if annotation is uuid.UUID:
        return _native_uuid(value)
    if annotation is datetime:
        parsed = _native_datetime(value)
        if parsed is None:
            raise ValueError
        return parsed
    if annotation is int:
        if field_name == "eviction_priority":
            return _native_priority(value, reason=f"{field_name}_invalid")
        return _native_int(value, reason=f"{field_name}_invalid")
    if annotation is bool:
        if type(value) is not bool:
            raise ValueError
        return value
    if annotation is str:
        if type(value) is not str:
            raise ValueError
        if field_name in {
            "generation",
            "source_generation",
            "drain_generation",
            "control_epoch",
            "runtime_epoch",
            "observed_generation",
        }:
            validate_canonical_positive_int64(value)
        return value
    if origin is tuple:
        args = get_args(annotation)
        if len(args) != 2 or args[1] is not Ellipsis or type(value) is not list:
            raise ValueError
        return tuple(
            _decode_native_subject_field(
                args[0],
                item,
                field_name=field_name,
            )
            for item in value
        )
    raise ValueError


def _native_preparation(
    result: object,
    subject_type: type[_NativePreparedT],
) -> _NativePreparedT:
    if type(result) is not dict:
        raise GPUAuthorityUnavailable()
    if result.get("status") == "blocked":
        if set(result) != {"status", "reason"}:
            raise GPUAuthorityUnavailable()
        reason = result["reason"]
        if type(reason) is not str or not reason:
            raise GPUAuthorityUnavailable()
        raise _GPUProofInvalid(reason)
    if result.get("status") != "prepared" or set(result) != {
        "status",
        "preparation",
    }:
        raise GPUAuthorityUnavailable()
    preparation = result["preparation"]
    fields = dataclass_fields(subject_type)
    if type(preparation) is not dict or set(preparation) != {
        item.name for item in fields
    }:
        raise GPUAuthorityUnavailable()
    try:
        annotations = get_type_hints(subject_type)
        values = {
            item.name: _decode_native_subject_field(
                annotations[item.name],
                preparation[item.name],
                field_name=item.name,
            )
            for item in fields
        }
        return subject_type(**values)
    except GPUAuthorityUnavailable:
        raise
    except (KeyError, TypeError, ValueError):
        raise GPUAuthorityUnavailable() from None


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
    memberships: tuple[GPUProofMembership, ...]
    fences: dict[uuid.UUID, GPUProofFence]
    registries: dict[uuid.UUID, GPUProofRegistry]
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
    membership: GPUProofMembership,
    fence: GPUProofFence,
    registry: GPUProofRegistry,
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
    membership: GPUProofMembership,
    fence: GPUProofFence,
    registry: GPUProofRegistry,
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
    membership: GPUProofMembership,
    fence: GPUProofFence,
    registry: GPUProofRegistry,
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
    data: GPUAuthorityData,
    *,
    backend_id: str,
    gpu_resource_id: str,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPUResidentRuntimeSubject:
    """Read one strict Resident subject from one Native snapshot."""

    backend_registry_id = _validate_runtime_subject_inputs(
        backend_id,
        gpu_resource_id,
        evidence_ttl,
    )
    result = await data.step(
        "runtime_subject",
        {
            "backend_registry_id": str(backend_registry_id),
            "gpu_resource_id": gpu_resource_id,
        },
    )
    try:
        facts = _parse_native_proof_subject(result)
        subject = _validate_gpu_resident_runtime_subject(
            facts.membership,
            facts.fence,
            facts.registry,
            db_now=facts.db_now,
            evidence_ttl=evidence_ttl,
        )
        _validate_native_subject_projection(facts, subject)
        return subject
    except _GPUProofInvalid as exc:
        raise GPUResidentRuntimeSubjectError(exc.reason) from None


async def _read_gpu_eviction_runtime_subject(
    data: GPUAuthorityData,
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
    result = await data.step(
        "idle_eviction_subject" if require_idle else "busy_eviction_subject",
        {
            "backend_registry_id": str(backend_registry_id),
            "gpu_resource_id": gpu_resource_id,
            "expected_generation": expected_generation,
            "challenge": challenge,
        },
    )
    try:
        facts = _parse_native_proof_subject(result)
        subject = _validate_gpu_idle_eviction_runtime_subject(
            facts.membership,
            facts.fence,
            facts.registry,
            db_now=facts.db_now,
            evidence_ttl=evidence_ttl,
            expected_generation=expected_generation,
            challenge=challenge,
            require_idle=require_idle,
        )
        _validate_native_subject_projection(facts, subject)
        return subject
    except _GPUProofInvalid as exc:
        raise error_type(exc.reason) from None


async def read_gpu_idle_eviction_runtime_subject(
    data: GPUAuthorityData,
    *,
    backend_id: str,
    gpu_resource_id: str,
    expected_generation: str,
    challenge: str,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPUIdleEvictionRuntimeSubject:
    """Read one strict idle Resident victim from one Native snapshot."""

    return await _read_gpu_eviction_runtime_subject(
        data,
        backend_id=backend_id,
        gpu_resource_id=gpu_resource_id,
        expected_generation=expected_generation,
        challenge=challenge,
        evidence_ttl=evidence_ttl,
        require_idle=True,
        error_type=GPUIdleEvictionRuntimeSubjectError,
    )


async def read_gpu_busy_eviction_runtime_subject(
    data: GPUAuthorityData,
    *,
    backend_id: str,
    gpu_resource_id: str,
    expected_generation: str,
    challenge: str,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPUIdleEvictionRuntimeSubject:
    """Read one strict Resident victim while allowing live work to drain."""

    return await _read_gpu_eviction_runtime_subject(
        data,
        backend_id=backend_id,
        gpu_resource_id=gpu_resource_id,
        expected_generation=expected_generation,
        challenge=challenge,
        evidence_ttl=evidence_ttl,
        require_idle=False,
        error_type=GPUBusyEvictionRuntimeSubjectError,
    )


async def read_gpu_cold_runtime_subject(
    data: GPUAuthorityData,
    *,
    backend_id: str,
    gpu_resource_id: str,
    expected_challenge: str | None = None,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPUColdRuntimeSubject:
    """Read one strict idle-unloaded subject from one Native snapshot."""

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
    result = await data.step(
        "cold_runtime_subject",
        {
            "backend_registry_id": str(backend_registry_id),
            "gpu_resource_id": gpu_resource_id,
            "expected_challenge": expected_challenge,
        },
    )
    try:
        facts = _parse_native_proof_subject(result)
        subject = _validate_gpu_cold_runtime_subject(
            facts.membership,
            facts.fence,
            facts.registry,
            db_now=facts.db_now,
            evidence_ttl=evidence_ttl,
            expected_challenge=expected_challenge,
        )
        _validate_native_subject_projection(facts, subject)
        return subject
    except _GPUProofInvalid as exc:
        raise GPUColdRuntimeSubjectError(exc.reason) from None


async def _lock_gpu_idle_eviction_runtime_subject(
    data: GPUAuthorityData,
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
        data,
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
    data_provider: GPUAuthorityDataProvider,
    expected_subject: GPUIdleEvictionRuntimeSubject,
    *,
    token_expires_at: datetime,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPUPreparedIdleEvictionRuntimeSubject:
    """Atomically advance one exact victim generation and token horizon."""

    _validate_token_expiry(token_expires_at)
    _validate_runtime_subject_evidence_ttl(evidence_ttl)
    error_type = (
        GPUIdleEvictionRuntimeSubjectError
        if expected_subject.require_idle
        else GPUBusyEvictionRuntimeSubjectError
    )
    async with data_provider.transaction() as data:
        current_subject = await _lock_gpu_idle_eviction_runtime_subject(
            data,
            expected_subject,
            evidence_ttl=evidence_ttl,
        )
        if token_expires_at <= current_subject.db_now:
            raise error_type("token_expiry_not_in_future")
        result = await data.step(
            "prepare_idle_eviction_generation",
            {
                "expected_subject": _native_subject_input(current_subject),
                "token_expires_at": _canonical_proof_timestamp(token_expires_at),
            },
        )
        try:
            prepared = _native_preparation(
                result,
                GPUPreparedIdleEvictionRuntimeSubject,
            )
        except _GPUProofInvalid as exc:
            raise error_type(exc.reason) from None
        if (
            prepared.backend_registry_id != current_subject.backend_registry_id
            or prepared.gpu_resource_id != current_subject.gpu_resource_id
            or prepared.membership_epoch != current_subject.membership_epoch
            or prepared.budget_mb != current_subject.budget_mb
            or prepared.eviction_priority != current_subject.eviction_priority
            or prepared.max_concurrency != current_subject.max_concurrency
            or prepared.boot_id != current_subject.boot_id
            or prepared.source_generation != current_subject.generation
            or int(prepared.generation) <= current_subject.generation_high_water
            or prepared.pool_ids != current_subject.pool_ids
            or prepared.control_epoch != current_subject.control_epoch
            or prepared.runtime_epoch != current_subject.runtime_epoch
            or prepared.token_expires_at != token_expires_at.astimezone(UTC)
            or prepared.require_idle != current_subject.require_idle
            or prepared.db_now < current_subject.db_now
            or prepared.token_expires_at <= prepared.db_now
        ):
            raise error_type("runtime_subject_changed")
        return prepared


def _validate_eviction_cancel_preparation(
    subject: GPUPreparedEvictionCancelRuntimeSubject,
    *,
    db_now: datetime,
    owner_id: str,
    expected_subject: GPUPreparedIdleEvictionRuntimeSubject | None = None,
    owner_hard_deadline_ms: int | None = None,
    token_expires_at: datetime | None = None,
) -> None:
    try:
        source_generation = int(
            _parse_canonical_positive_int64(
                subject.source_generation,
                reason="cancel_intent_generation_invalid",
            )
        )
        drain_generation = int(
            _parse_canonical_positive_int64(
                subject.drain_generation,
                reason="cancel_intent_generation_invalid",
            )
        )
        generation = int(
            _parse_canonical_positive_int64(
                subject.generation,
                reason="cancel_intent_generation_invalid",
            )
        )
        membership_epoch = _strict_nonnegative_int64(
            subject.membership_epoch,
            reason="cancel_intent_membership_invalid",
        )
        _parse_canonical_positive_int64(
            subject.control_epoch,
            reason="cancel_intent_epoch_invalid",
        )
        _parse_canonical_positive_int64(
            subject.runtime_epoch,
            reason="cancel_intent_epoch_invalid",
        )
    except _GPUProofInvalid:
        raise
    if (
        drain_generation <= source_generation
        or generation <= drain_generation
        or membership_epoch <= 0
    ):
        raise _GPUProofInvalid("cancel_intent_generation_invalid")
    if (
        subject.operation != GPU_EVICTION_OPERATION
        or subject.owner_id != owner_id
        or not subject.owner_id
        or len(subject.owner_id) > 256
        or not subject.jti.startswith("transition:")
        or len(subject.jti) > 256
        or not subject.boot_id
        or len(subject.boot_id) > 128
    ):
        raise _GPUProofInvalid("cancel_intent_identity_changed")
    if (
        not subject.pool_ids
        or subject.pool_ids != tuple(sorted(set(subject.pool_ids)))
        or any(not item or item.strip() != item for item in subject.pool_ids)
    ):
        raise _GPUProofInvalid("cancel_intent_pool_ids_invalid")
    try:
        validate_gpu_resource_id(subject.gpu_resource_id)
    except (TypeError, ValueError) as exc:
        raise _GPUProofInvalid("cancel_intent_identity_invalid") from exc
    try:
        owner_hard_deadline = datetime.fromtimestamp(
            subject.owner_hard_deadline_ms / 1000,
            UTC,
        )
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise _GPUProofInvalid("cancel_intent_owner_deadline_invalid") from exc
    if (
        isinstance(subject.owner_hard_deadline_ms, bool)
        or subject.owner_hard_deadline_ms <= 0
        or subject.owner_hard_deadline_ms > _MAX_POSITIVE_INT64
        or owner_hard_deadline <= db_now
        or subject.token_expires_at > owner_hard_deadline
    ):
        raise _GPUProofInvalid("cancel_intent_owner_deadline_invalid")
    if subject.token_expires_at <= db_now:
        raise _GPUProofInvalid("cancel_intent_expired")
    if expected_subject is not None:
        if (
            subject.backend_registry_id != expected_subject.backend_registry_id
            or subject.gpu_resource_id != expected_subject.gpu_resource_id
            or subject.membership_epoch != expected_subject.membership_epoch
            or subject.budget_mb != expected_subject.budget_mb
            or subject.eviction_priority != expected_subject.eviction_priority
            or subject.max_concurrency != expected_subject.max_concurrency
            or subject.boot_id != expected_subject.boot_id
            or subject.source_generation != expected_subject.source_generation
            or subject.drain_generation != expected_subject.generation
            or subject.pool_ids != expected_subject.pool_ids
            or subject.control_epoch != expected_subject.control_epoch
            or subject.runtime_epoch != expected_subject.runtime_epoch
            or subject.drain_token_expires_at
            != expected_subject.token_expires_at.astimezone(UTC)
            or expected_subject.require_idle
            or owner_hard_deadline_ms != subject.owner_hard_deadline_ms
            or token_expires_at is None
            or subject.token_expires_at != token_expires_at.astimezone(UTC)
            or subject.db_now < expected_subject.db_now
        ):
            raise _GPUProofInvalid("cancel_intent_conflict")
    elif not subject.idempotent:
        raise _GPUProofInvalid("cancel_intent_identity_changed")


async def prepare_gpu_eviction_cancel_runtime_generation(
    data_provider: GPUAuthorityDataProvider,
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
    async with data_provider.transaction() as data:
        locked = await _lock_gpu_resource_proof_domain(
            data,
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
        durable_reason = _eviction_terminal_durable_reason(
            membership,
            fence,
            registry,
            expected_subject,
        )
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
            raise ValueError("owner_hard_deadline_ms must fit a UTC datetime") from exc
        if owner_hard_deadline <= locked.db_now:
            raise GPUEvictionCancelRuntimeSubjectError(
                "transition_owner_deadline_expired"
            )
        if token_expires_at.astimezone(UTC) > owner_hard_deadline:
            raise GPUEvictionCancelRuntimeSubjectError(
                "token_expiry_exceeds_owner_deadline"
            )
        result = await data.step(
            "prepare_eviction_cancel_generation",
            {
                "phase": "prepare",
                "expected_subject": _native_subject_input(expected_subject),
                "owner_id": owner_id,
                "owner_hard_deadline_ms": owner_hard_deadline_ms,
                "token_expires_at": _canonical_proof_timestamp(token_expires_at),
            },
        )
        try:
            prepared = _native_preparation(
                result,
                GPUPreparedEvictionCancelRuntimeSubject,
            )
            if prepared.db_now < locked.db_now:
                raise _GPUProofInvalid("cancel_intent_owner_deadline_invalid")
            _validate_eviction_cancel_preparation(
                prepared,
                db_now=prepared.db_now,
                owner_id=owner_id,
                expected_subject=expected_subject,
                owner_hard_deadline_ms=owner_hard_deadline_ms,
                token_expires_at=token_expires_at,
            )
        except _GPUProofInvalid as exc:
            raise GPUEvictionCancelRuntimeSubjectError(exc.reason) from None
        return prepared


async def read_gpu_eviction_cancel_runtime_subject(
    data: GPUAuthorityData,
    *,
    backend_id: str,
    gpu_resource_id: str,
    owner_id: str,
) -> GPUPreparedEvictionCancelRuntimeSubject:
    """Recover one still-live exact cancel intent from Native facts."""

    backend_registry_id = _validate_runtime_subject_inputs(
        backend_id,
        gpu_resource_id,
        _HEALTH_EVIDENCE_MAX_AGE,
        error_type=GPUEvictionCancelRuntimeSubjectError,
    )
    if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 256:
        raise ValueError("owner_id must be a non-empty string up to 256 chars")
    result = await data.step(
        "prepare_eviction_cancel_generation",
        {
            "phase": "recover",
            "backend_registry_id": str(backend_registry_id),
            "gpu_resource_id": gpu_resource_id,
            "owner_id": owner_id,
        },
    )
    try:
        subject = _native_preparation(
            result,
            GPUPreparedEvictionCancelRuntimeSubject,
        )
        _validate_eviction_cancel_preparation(
            subject,
            db_now=subject.db_now,
            owner_id=owner_id,
        )
        if subject.backend_registry_id != backend_registry_id:
            raise _GPUProofInvalid("cancel_intent_identity_changed")
    except _GPUProofInvalid as exc:
        raise GPUEvictionCancelRuntimeSubjectError(exc.reason) from None
    return subject


async def _lock_gpu_cold_runtime_subject(
    data: GPUAuthorityData,
    expected_subject: GPUColdRuntimeSubject,
    *,
    evidence_ttl: timedelta,
) -> GPUColdRuntimeSubject:
    locked = await _lock_gpu_resource_proof_domain(
        data,
        expected_subject.gpu_resource_id,
    )
    membership = next(
        (
            item
            for item in locked.memberships
            if item.backend_registry_id == expected_subject.backend_registry_id
            and item.gpu_resource_id == expected_subject.gpu_resource_id
            and item.membership_epoch == expected_subject.membership_epoch
            and item.state == "active"
        ),
        None,
    )
    fence = locked.fences.get(expected_subject.backend_registry_id)
    registry = locked.registries.get(expected_subject.backend_registry_id)
    if membership is None:
        raise GPUColdRuntimeSubjectError("membership_changed")
    if fence is None or registry is None:
        raise GPUColdRuntimeSubjectError("runtime_subject_missing")
    try:
        current_subject = _validate_gpu_cold_runtime_subject(
            membership,
            fence,
            registry,
            db_now=locked.db_now,
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
    data_provider: GPUAuthorityDataProvider,
    expected_subject: GPUColdRuntimeSubject,
    *,
    token_expires_at: datetime,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPUPreparedColdRuntimeSubject:
    """Atomically reserve one cold generation and its token horizon."""

    _validate_token_expiry(token_expires_at)
    _validate_runtime_subject_evidence_ttl(evidence_ttl)
    async with data_provider.transaction() as data:
        current_subject = await _lock_gpu_cold_runtime_subject(
            data,
            expected_subject,
            evidence_ttl=evidence_ttl,
        )
        if token_expires_at <= current_subject.db_now:
            raise GPUColdRuntimeSubjectError("token_expiry_not_in_future")
        result = await data.step(
            "prepare_cold_generation",
            {
                "expected_subject": _native_subject_input(current_subject),
                "token_expires_at": _canonical_proof_timestamp(token_expires_at),
            },
        )
        try:
            prepared = _native_preparation(result, GPUPreparedColdRuntimeSubject)
        except _GPUProofInvalid as exc:
            raise GPUColdRuntimeSubjectError(exc.reason) from None
        if (
            prepared.backend_registry_id != current_subject.backend_registry_id
            or prepared.gpu_resource_id != current_subject.gpu_resource_id
            or prepared.membership_epoch != current_subject.membership_epoch
            or prepared.budget_mb != current_subject.budget_mb
            or prepared.eviction_priority != current_subject.eviction_priority
            or prepared.max_concurrency != current_subject.max_concurrency
            or prepared.boot_id != current_subject.boot_id
            or prepared.observed_generation != current_subject.observed_generation
            or int(prepared.generation) <= current_subject.generation_high_water
            or prepared.control_epoch != current_subject.control_epoch
            or prepared.runtime_epoch != current_subject.runtime_epoch
            or prepared.token_expires_at != token_expires_at.astimezone(UTC)
            or prepared.db_now < current_subject.db_now
            or prepared.token_expires_at <= prepared.db_now
        ):
            raise GPUColdRuntimeSubjectError("runtime_subject_changed")
        return prepared


async def record_gpu_resident_runtime_token_expiry(
    data_provider: GPUAuthorityDataProvider,
    expected_subject: GPUResidentRuntimeSubject,
    *,
    token_expires_at: datetime,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> datetime:
    """Revalidate one subject under ordered locks and commit its token horizon."""

    _validate_token_expiry(token_expires_at)
    _validate_runtime_subject_evidence_ttl(evidence_ttl)
    async with data_provider.transaction() as data:
        locked = await _lock_gpu_resource_proof_domain(
            data,
            expected_subject.gpu_resource_id,
        )
        membership = next(
            (
                item
                for item in locked.memberships
                if item.backend_registry_id == expected_subject.backend_registry_id
                and item.gpu_resource_id == expected_subject.gpu_resource_id
                and item.membership_epoch == expected_subject.membership_epoch
                and item.state == "active"
            ),
            None,
        )
        fence = locked.fences.get(expected_subject.backend_registry_id)
        registry = locked.registries.get(expected_subject.backend_registry_id)
        if membership is None:
            raise GPUResidentRuntimeSubjectError("membership_changed")
        if fence is None or registry is None:
            raise GPUResidentRuntimeSubjectError("runtime_subject_missing")
        try:
            current_subject = _validate_gpu_resident_runtime_subject(
                membership,
                fence,
                registry,
                db_now=locked.db_now,
                evidence_ttl=evidence_ttl,
            )
        except _GPUProofInvalid as exc:
            raise GPUResidentRuntimeSubjectError(exc.reason) from None
        if _runtime_subject_identity(current_subject) != _runtime_subject_identity(
            expected_subject
        ):
            raise GPUResidentRuntimeSubjectError("runtime_subject_changed")
        if token_expires_at <= locked.db_now:
            raise GPUResidentRuntimeSubjectError("token_expiry_not_in_future")
        result = await data.step(
            "record_runtime_token_expiry",
            {
                "expected_subject": _native_subject_input(current_subject),
                "token_expires_at": _canonical_proof_timestamp(token_expires_at),
            },
        )
        if type(result) is dict and result.get("status") == "blocked":
            if set(result) != {"status", "reason"}:
                raise GPUAuthorityUnavailable()
            reason = result["reason"]
            if type(reason) is not str or not reason:
                raise GPUAuthorityUnavailable()
            raise GPUResidentRuntimeSubjectError(reason)
        if (
            type(result) is not dict
            or set(result)
            != {
                "status",
                "token_expires_at",
            }
            or result["status"] != "recorded"
        ):
            raise GPUAuthorityUnavailable()
        try:
            recorded = _native_datetime(result["token_expires_at"])
            if recorded is None or recorded != token_expires_at.astimezone(UTC):
                raise ValueError
        except (TypeError, ValueError):
            raise GPUAuthorityUnavailable() from None
        return recorded


async def _lock_gpu_resource_proof_domain(
    data: GPUAuthorityData,
    resource_id: str,
    *,
    wait_for_lock: bool = True,
) -> _LockedGPUProofDomain:
    """Read the ordered, resource-locked Native domain projection.

    The Native action owns the advisory/membership/fence lock order and keeps
    registry reads non-locking, so a registry writer never waits in reverse order.
    """

    result = await data.step(
        "proof_domain",
        {"resource_id": resource_id, "wait_for_lock": wait_for_lock},
    )
    if type(result) is dict and result.get("status") == "blocked":
        reason = result.get("reason")
        if type(reason) is not str or not reason:
            raise GPUAuthorityUnavailable()
        if reason == "gpu_resource_busy":
            raise _GPUProofInvalid(reason)
        raise GPUAuthorityUnavailable()
    return parse_gpu_proof_domain(result)


def _eviction_terminal_durable_reason(
    membership: GPUProofMembership,
    fence: GPUProofFence,
    registry: GPUProofRegistry,
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
    # a gap above this owner. Exact Redis owner/generation and fresh health still
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
        registry.gpu_resource_id != expected_subject.gpu_resource_id
        or registry.vram_budget_mb != expected_subject.budget_mb
        or registry.eviction_priority != expected_subject.eviction_priority
        or _registry_gpu_max_concurrency(registry.extra_params)
        != expected_subject.max_concurrency
    ):
        return "registry_claim_changed"
    return None


def _read_exact_gpu_eviction_residency(
    membership: GPUProofMembership,
    registry: GPUProofRegistry,
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


def _classify_gpu_eviction_cancel(
    membership: GPUProofMembership,
    registry: GPUProofRegistry,
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
    membership: GPUProofMembership,
    registry: GPUProofRegistry,
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
    data: GPUAuthorityData,
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
    result = await data.step(
        "eviction_drain_health",
        {
            "expected_subject": _native_subject_input(expected_subject),
            "challenge": challenge,
        },
    )
    try:
        facts = _parse_native_proof_domain_subject(result)
        durable_reason = _eviction_terminal_durable_reason(
            facts.membership,
            facts.fence,
            facts.registry,
            expected_subject,
        )
    except _GPUProofInvalid as exc:
        return GPUEvictionDrainHealth(status="uncertain", reason=exc.reason)
    if durable_reason is not None:
        return GPUEvictionDrainHealth(status="uncertain", reason=durable_reason)
    try:
        residency = _read_exact_gpu_eviction_residency(
            facts.membership,
            facts.registry,
            expected_subject,
            challenge=challenge,
            db_now=facts.db_now,
            evidence_ttl=evidence_ttl,
        )
        ready = _gpu_draining_residency_ready(residency)
    except _GPUProofInvalid as exc:
        return GPUEvictionDrainHealth(status="uncertain", reason=exc.reason)
    return GPUEvictionDrainHealth(
        status="ready_to_unload" if ready else "draining_busy",
        reason="ready_to_unload" if ready else "draining_busy",
        active_requests=residency.active_requests,
        builders=residency.builders,
        borrowers=residency.borrowers,
    )


async def commit_gpu_eviction_phase_from_health(
    data_provider: GPUAuthorityDataProvider,
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
    async with data_provider.transaction() as data:
        result = await data.step(
            "eviction_phase_from_health",
            {
                "expected_subject": _native_subject_input(expected_subject),
                "phase": phase,
                "challenge": challenge,
            },
        )
        try:
            facts = _parse_native_proof_domain_subject(result)
        except _GPUProofInvalid as exc:
            return GPUEvictionCommitResult(
                status="stale",
                state=GPUAllocationState.UNKNOWN,
                reason=exc.reason,
            )
        durable_reason = _eviction_terminal_durable_reason(
            facts.membership,
            facts.fence,
            facts.registry,
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
                facts.membership,
                facts.registry,
                expected_subject,
                phase=phase,
                challenge=challenge,
                db_now=facts.db_now,
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
    data_provider: GPUAuthorityDataProvider,
    store: GPUArbiterStore,
    expected_subject: GPUPreparedEvictionCancelRuntimeSubject,
    *,
    ack_confirmed: bool,
    challenge: str | None,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPUEvictionCommitResult:
    """Commit one passive cancel proof; uncertainty can only tighten to Unknown.

    This helper deliberately does not send RESUME or choose between cancel and
    unload. The authority must install a Redis branch fence before either active
    branch is wired.
    """

    if not isinstance(ack_confirmed, bool):
        raise ValueError("ack_confirmed must be a boolean")
    if challenge is not None and _GPU_HEALTH_CHALLENGE_RE.fullmatch(challenge) is None:
        raise ValueError("challenge must be 64 lowercase hexadecimal characters")
    _validate_runtime_subject_evidence_ttl(evidence_ttl)
    async with data_provider.transaction() as data:
        result = await data.step(
            "eviction_cancel_from_health",
            {
                "expected_subject": _native_subject_input(expected_subject),
                "ack_confirmed": ack_confirmed,
                "challenge": challenge,
            },
        )
        try:
            facts = _parse_native_proof_domain_subject(result)
        except _GPUProofInvalid as exc:
            return GPUEvictionCommitResult(
                status="stale",
                state=GPUAllocationState.UNKNOWN,
                reason=exc.reason,
            )
        durable_reason = _eviction_terminal_durable_reason(
            facts.membership,
            facts.fence,
            facts.registry,
            expected_subject,
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
                facts.membership,
                facts.registry,
                expected_subject,
                challenge=challenge,
                db_now=facts.db_now,
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
    membership: GPUProofMembership,
    fence: GPUProofFence,
    registry: GPUProofRegistry,
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
    membership: GPUProofMembership,
    registry: GPUProofRegistry,
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
    data_provider: GPUAuthorityDataProvider,
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
    async with data_provider.transaction() as data:
        result = await data.step(
            "cold_terminal_from_health",
            {
                "expected_subject": _native_subject_input(expected_subject),
                "challenge": challenge,
            },
        )
        try:
            facts = _parse_native_proof_domain_subject(result)
        except _GPUProofInvalid as exc:
            return GPUColdTerminalCommitResult(
                status="stale",
                state=GPUAllocationState.UNKNOWN,
                reason=exc.reason,
            )
        durable_reason = _cold_terminal_durable_reason(
            facts.membership,
            facts.fence,
            facts.registry,
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
                facts.membership,
                facts.registry,
                expected_subject,
                challenge=challenge,
                db_now=facts.db_now,
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
    memberships: Iterable[GPUProofMembership],
) -> tuple[GPUBackendDomainMember, ...]:
    return tuple(
        GPUBackendDomainMember(
            backend_id=str(item.backend_registry_id),
            membership_epoch=item.membership_epoch,
            state=item.state,  # type: ignore[arg-type]
        )
        for item in memberships
    )
