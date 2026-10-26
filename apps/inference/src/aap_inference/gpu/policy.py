"""Cycle-safe GPU arbitration policy: claim validation, shadow arbitration, mode resolution.

Pure policy logic that depends only on :mod:`gpu_arbitration.contracts` and stdlib /
config. It does NOT import ``ml_client`` or touch the Redis ledger, so ``ml_client`` can
safely depend on it without forming a cycle. Extracted verbatim from ``gpu_arbiter.py``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from aap_inference.gpu.contracts import GPUConfigDiagnostic
from aap_inference.gpu.resource import validate_gpu_resource_id
from aap_inference.gpu.settings import (
    GPUArbiterMode,
    GPUAuthoritySettings as Settings,
)


_HEALTH_EVIDENCE_MAX_AGE = timedelta(minutes=3)


_HEALTH_EVIDENCE_FUTURE_SKEW = timedelta(minutes=1)


_CANONICAL_POSITIVE_INT64_RE = re.compile(r"[1-9][0-9]{0,18}\Z")


_MAX_POSITIVE_INT64 = 9_223_372_036_854_775_807


@dataclass(frozen=True)
class GPUShadowCandidate:
    """One safe-looking candidate in a P2 snapshot, not an eviction selection."""

    backend_id: str
    vram_budget_mb: int
    eviction_priority: int
    generation: str


@dataclass(frozen=True)
class GPUShadowDecision:
    """Non-authoritative P2 decision emitted for observability only."""

    decision: Literal["would-admit", "would-evict", "would-reject"]
    reason: str
    operation: str
    backend_id: str
    resource_id: str | None
    global_mode: Literal["off", "observe", "enforce"]
    desired_mode: Literal["off", "observe", "enforce"]
    effective_mode: Literal["off", "observe", "enforce"]
    allocatable_mb: int | None
    committed_before_mb: int
    requested_increment_mb: int
    projected_mb: int
    shortfall_mb: int
    candidates: tuple[GPUShadowCandidate, ...] = ()
    uncertain_backend_ids: tuple[str, ...] = ()
    authoritative: bool = False
    candidate_order_authoritative: bool = False
    unmanaged_workload: bool = True


def effective_gpu_arbiter_mode(resource_id: str, *, config: Settings) -> GPUArbiterMode:
    """P2b can make observe effective; enforce stays off until P3/P4 handshakes."""

    desired = config.gpu_arbiter_desired_mode(resource_id)
    if desired is GPUArbiterMode.OBSERVE:
        return GPUArbiterMode.OBSERVE
    return GPUArbiterMode.OFF


def any_gpu_resource_effectively_enforced(*, config: Settings) -> bool:
    """Return whether at least one configured resource is truly enforced."""

    return any(
        effective_gpu_arbiter_mode(resource_id, config=config) is GPUArbiterMode.ENFORCE
        for resource_id in config.gpu_arbiter_resources
    )


def gpu_shadow_observation_enabled(
    resource_id: str | None, *, config: Settings
) -> bool:
    """Fast, side-effect-free guard used before opening a shadow DB session."""

    if config.gpu_arbiter_mode is GPUArbiterMode.OFF:
        return False
    if resource_id is None:
        return config.gpu_arbiter_mode is GPUArbiterMode.OBSERVE
    if resource_id not in config.gpu_arbiter_resources:
        # observe 仍需暴露未知/畸形 claim；enforce 下未知资源继续安全回落 off。
        return config.gpu_arbiter_mode is GPUArbiterMode.OBSERVE
    return (
        effective_gpu_arbiter_mode(resource_id, config=config) is GPUArbiterMode.OBSERVE
    )


class GPUClaimConfigurationError(ValueError):
    """A registry claim cannot be represented safely by current resource config."""

    def __init__(self, diagnostics: list[GPUConfigDiagnostic]) -> None:
        self.diagnostics = diagnostics
        message = diagnostics[0].message if diagnostics else "GPU 资源配置无效"
        super().__init__(message)


def _diag(
    code: str,
    level: str,
    message: str,
    *,
    field: str | None = None,
    resource_id: str | None = None,
    backend_id: Any = None,
) -> GPUConfigDiagnostic:
    return GPUConfigDiagnostic(
        code=code,
        level=level,
        message=message,
        field=field,
        resource_id=resource_id,
        backend_id=backend_id,
    )


def _claim_shape_diagnostics(
    gpu_resource_id: str | None,
    vram_budget_mb: int | None,
    *,
    backend_id: Any = None,
) -> list[GPUConfigDiagnostic]:
    diagnostics: list[GPUConfigDiagnostic] = []
    if (gpu_resource_id is None) != (vram_budget_mb is None):
        diagnostics.append(
            _diag(
                "gpu_claim_incomplete",
                "blocker",
                "gpu_resource_id 与 vram_budget_mb 必须同时设置或同时为 null",
                field="gpu_resource_id",
                resource_id=gpu_resource_id,
                backend_id=backend_id,
            )
        )
    if gpu_resource_id is not None:
        try:
            validate_gpu_resource_id(gpu_resource_id)
        except ValueError as exc:
            diagnostics.append(
                _diag(
                    "gpu_resource_id_invalid",
                    "blocker",
                    str(exc),
                    field="gpu_resource_id",
                    resource_id=gpu_resource_id,
                    backend_id=backend_id,
                )
            )
    if vram_budget_mb is not None and (
        isinstance(vram_budget_mb, bool)
        or not isinstance(vram_budget_mb, int)
        or vram_budget_mb <= 0
    ):
        diagnostics.append(
            _diag(
                "vram_budget_invalid",
                "blocker",
                "vram_budget_mb 必须是正整数 MiB",
                field="vram_budget_mb",
                resource_id=gpu_resource_id,
                backend_id=backend_id,
            )
        )
    return diagnostics


def validate_gpu_claim(
    gpu_resource_id: str | None,
    vram_budget_mb: int | None,
    *,
    extra_params: Mapping[str, Any] | None = None,
    config: Settings,
) -> None:
    """Reject only per-backend blockers; aggregate oversubscription stays a warning."""

    diagnostics = _claim_shape_diagnostics(gpu_resource_id, vram_budget_mb)
    if gpu_resource_id is not None and not diagnostics:
        resource = config.gpu_arbiter_resources.get(gpu_resource_id)
        if config.gpu_arbiter_config_errors:
            diagnostics.append(
                _diag(
                    "gpu_resources_config_invalid",
                    "blocker",
                    "GPU_ARBITER_RESOURCES_JSON 无法解析："
                    f"{config.gpu_arbiter_config_errors[0]}",
                    field="gpu_resource_id",
                    resource_id=gpu_resource_id,
                )
            )
        elif resource is None:
            diagnostics.append(
                _diag(
                    "gpu_resource_unknown",
                    "blocker",
                    f"未在 GPU_ARBITER_RESOURCES_JSON 中找到 {gpu_resource_id}",
                    field="gpu_resource_id",
                    resource_id=gpu_resource_id,
                )
            )
        elif vram_budget_mb is not None and vram_budget_mb > resource.allocatable_mb:
            diagnostics.append(
                _diag(
                    "vram_budget_exceeds_allocatable",
                    "blocker",
                    f"预算 {vram_budget_mb} MiB 超过该资源可分配容量 "
                    f"{resource.allocatable_mb} MiB",
                    field="vram_budget_mb",
                    resource_id=gpu_resource_id,
                )
            )
        if extra_params is not None and "max_concurrency" in extra_params:
            raw_limit = extra_params["max_concurrency"]
            valid_limit = (
                isinstance(raw_limit, int)
                and not isinstance(raw_limit, bool)
                and 1 <= raw_limit <= 10000
            ) or (
                isinstance(raw_limit, str)
                and re.fullmatch(r"[1-9][0-9]{0,4}", raw_limit) is not None
                and int(raw_limit) <= 10000
            )
            if not valid_limit:
                diagnostics.append(
                    _diag(
                        "gpu_max_concurrency_invalid",
                        "blocker",
                        "extra_params.max_concurrency 必须是 1 到 10000 的整数",
                        field="extra_params.max_concurrency",
                        resource_id=gpu_resource_id,
                    )
                )
    if diagnostics:
        raise GPUClaimConfigurationError(diagnostics)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _health_evidence_is_trusted(backend: Any, *, now: datetime | None = None) -> bool:
    if getattr(backend, "state", None) != "connected":
        return False
    checked_at = getattr(backend, "last_checked_at", None)
    if not isinstance(checked_at, datetime) or checked_at.tzinfo is None:
        return False
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    age = current.astimezone(UTC) - checked_at.astimezone(UTC)
    if age > _HEALTH_EVIDENCE_MAX_AGE or age < -_HEALTH_EVIDENCE_FUTURE_SKEW:
        return False
    return True


def _trusted_health_meta(
    backend: Any, *, now: datetime | None = None
) -> Mapping[str, Any]:
    if not _health_evidence_is_trusted(backend, now=now):
        return {}
    return _as_mapping(getattr(backend, "health_meta", None))


def _requires_gpu_claim(health_meta: Mapping[str, Any]) -> bool:
    compute = _as_mapping(health_meta.get("compute"))
    configured = compute.get("configured_device")
    configured = configured.strip().lower() if isinstance(configured, str) else None
    effective = compute.get("effective_device")
    effective = effective.strip().lower() if isinstance(effective, str) else None
    provider = compute.get("effective_provider")
    provider = provider.strip().lower() if isinstance(provider, str) else None
    capabilities = _as_mapping(health_meta.get("capabilities"))
    infra = capabilities.get("infra")
    infra = infra.strip().lower() if isinstance(infra, str) else None
    residency = _as_mapping(health_meta.get("residency"))
    return bool(
        configured == "gpu"
        or (configured and configured.startswith("cuda"))
        or (effective and effective.startswith("cuda"))
        or (provider and ("cuda" in provider or "tensorrt" in provider))
        or infra == "gpu"
        or residency.get("gpu_loaded") is True
    )


def _is_explicit_cpu_backend(
    health_meta: Mapping[str, Any], *, requires_gpu_claim: bool
) -> bool:
    compute = _as_mapping(health_meta.get("compute"))
    configured = compute.get("configured_device")
    return (
        isinstance(configured, str)
        and configured.strip().lower() == "cpu"
        and not requires_gpu_claim
    )


def backend_is_trusted_explicit_cpu(
    backend: Any, *, now: datetime | None = None
) -> bool:
    """Accept a null GPU claim only with fresh, connected explicit-CPU evidence."""

    health_meta = _trusted_health_meta(backend, now=now)
    requires_gpu_claim = _requires_gpu_claim(health_meta)
    return _is_explicit_cpu_backend(
        health_meta,
        requires_gpu_claim=requires_gpu_claim,
    )


def strict_gpu_loaded_evidence(health_meta: Mapping[str, Any]) -> bool | None:
    """Normalize residency without letting malformed ``false`` release capacity."""

    raw_residency = health_meta.get("residency")
    if not isinstance(raw_residency, Mapping):
        return None
    gpu_loaded = raw_residency.get("gpu_loaded")
    if gpu_loaded is True:
        return True
    if gpu_loaded is not False:
        return None
    builders = raw_residency.get("builders")
    borrowers = raw_residency.get("borrowers")
    if (
        isinstance(builders, bool)
        or not isinstance(builders, int)
        or builders != 0
        or isinstance(borrowers, bool)
        or not isinstance(borrowers, int)
        or borrowers != 0
    ):
        return None
    pools = raw_residency.get("pools")
    if not isinstance(pools, Mapping):
        return None
    for pool in pools.values():
        if not isinstance(pool, Mapping) or pool.get("resident") is not False:
            return None
    return False


def _is_strict_zero(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == 0


def _canonical_generation(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or _CANONICAL_POSITIVE_INT64_RE.fullmatch(value) is None
    ):
        return None
    if int(value) > _MAX_POSITIVE_INT64:
        return None
    return value


def _safe_shadow_candidate(
    backend: Any,
    health_meta: Mapping[str, Any],
    *,
    resource_id: str,
    requester_priority: int,
) -> GPUShadowCandidate | None:
    residency = health_meta.get("residency")
    if not isinstance(residency, Mapping):
        return None
    generation = _canonical_generation(residency.get("generation"))
    identity = residency.get("identity")
    backend_id = str(getattr(backend, "id", ""))
    if (
        residency.get("state") != "resident"
        or residency.get("gpu_loaded") is not True
        or residency.get("evictable") is not True
        or residency.get("lifecycle_gate") != "enforce"
        or generation is None
        or not isinstance(residency.get("boot_id"), str)
        or not residency.get("boot_id")
        or _canonical_generation(residency.get("control_epoch")) is None
        or not _is_strict_zero(residency.get("active_requests"))
        or not _is_strict_zero(residency.get("builders"))
        or not _is_strict_zero(residency.get("borrowers"))
        or residency.get("draining") is not False
        or not isinstance(identity, Mapping)
        or identity.get("audience") != "aap-gpu-lifecycle"
        or identity.get("backend_registry_id") != backend_id
        or identity.get("gpu_resource_id") != resource_id
    ):
        return None
    priority = getattr(backend, "eviction_priority", 0)
    budget = getattr(backend, "vram_budget_mb", None)
    if (
        isinstance(priority, bool)
        or not isinstance(priority, int)
        or priority > requester_priority
        or isinstance(budget, bool)
        or not isinstance(budget, int)
        or budget <= 0
    ):
        return None
    return GPUShadowCandidate(
        backend_id=backend_id,
        vram_budget_mb=budget,
        eviction_priority=priority,
        generation=generation,
    )


def _shadow_reject_for_claim(
    requester: Any,
    *,
    operation: str,
    reason: str,
    resource_id: str | None,
    global_mode: str,
    desired_mode: str,
    effective_mode: str,
    allocatable_mb: int | None = None,
) -> GPUShadowDecision:
    return GPUShadowDecision(
        decision="would-reject",
        reason=reason,
        operation=operation,
        backend_id=str(getattr(requester, "id", "")),
        resource_id=resource_id,
        global_mode=global_mode,  # type: ignore[arg-type]
        desired_mode=desired_mode,  # type: ignore[arg-type]
        effective_mode=effective_mode,  # type: ignore[arg-type]
        allocatable_mb=allocatable_mb,
        committed_before_mb=0,
        requested_increment_mb=0,
        projected_mb=0,
        shortfall_mb=0,
    )


def evaluate_gpu_shadow_decision(
    requester: Any,
    backends: Iterable[Any],
    *,
    operation: str,
    config: Settings,
    now: datetime | None = None,
) -> GPUShadowDecision | None:
    """Evaluate one observe-mode dispatch without changing business behavior."""

    resource_id = getattr(requester, "gpu_resource_id", None)
    budget = getattr(requester, "vram_budget_mb", None)
    requester_health = _trusted_health_meta(requester, now=now)
    requires_gpu_claim = _requires_gpu_claim(requester_health)

    if resource_id is None and budget is None:
        if config.gpu_arbiter_mode is not GPUArbiterMode.OBSERVE:
            return None
        if _is_explicit_cpu_backend(
            requester_health, requires_gpu_claim=requires_gpu_claim
        ):
            return None
        return _shadow_reject_for_claim(
            requester,
            operation=operation,
            reason="gpu_claim_missing_or_unverified",
            resource_id=None,
            global_mode=config.gpu_arbiter_mode.value,
            desired_mode="off",
            effective_mode="off",
        )

    if (
        not isinstance(resource_id, str)
        or not resource_id
        or (isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0)
    ):
        if config.gpu_arbiter_mode is not GPUArbiterMode.OBSERVE:
            return None
        return _shadow_reject_for_claim(
            requester,
            operation=operation,
            reason="gpu_claim_invalid",
            resource_id=resource_id if isinstance(resource_id, str) else None,
            global_mode=config.gpu_arbiter_mode.value,
            desired_mode="off",
            effective_mode="off",
        )

    resource = config.gpu_arbiter_resources.get(resource_id)
    if resource is None:
        if config.gpu_arbiter_mode is not GPUArbiterMode.OBSERVE:
            return None
        return _shadow_reject_for_claim(
            requester,
            operation=operation,
            reason="gpu_resource_unknown_or_invalid",
            resource_id=resource_id,
            global_mode=config.gpu_arbiter_mode.value,
            desired_mode="off",
            effective_mode="off",
        )

    desired = config.gpu_arbiter_desired_mode(resource_id)
    effective = effective_gpu_arbiter_mode(resource_id, config=config)
    if effective is not GPUArbiterMode.OBSERVE:
        return None
    if budget > resource.allocatable_mb:
        return _shadow_reject_for_claim(
            requester,
            operation=operation,
            reason="vram_budget_exceeds_allocatable",
            resource_id=resource_id,
            global_mode=config.gpu_arbiter_mode.value,
            desired_mode=desired.value,
            effective_mode=effective.value,
            allocatable_mb=resource.allocatable_mb,
        )

    rows_by_id: dict[str, Any] = {
        str(getattr(backend, "id", "")): backend
        for backend in backends
        if getattr(backend, "gpu_resource_id", None) == resource_id
    }
    requester_id = str(getattr(requester, "id", ""))
    rows_by_id[requester_id] = requester
    committed = 0
    uncertain: set[str] = set()
    loaded_by_id: dict[str, bool | None] = {}
    trusted_meta_by_id: dict[str, Mapping[str, Any]] = {}

    for backend_id, backend in rows_by_id.items():
        peer_budget = getattr(backend, "vram_budget_mb", None)
        if (
            isinstance(peer_budget, bool)
            or not isinstance(peer_budget, int)
            or peer_budget <= 0
        ):
            uncertain.add(backend_id)
            continue
        trusted = _health_evidence_is_trusted(backend, now=now)
        health_meta = _trusted_health_meta(backend, now=now)
        loaded = strict_gpu_loaded_evidence(health_meta) if trusted else None
        trusted_meta_by_id[backend_id] = health_meta
        loaded_by_id[backend_id] = loaded
        if loaded is not False:
            committed += peer_budget
        if loaded is None:
            uncertain.add(backend_id)

    requester_loaded = loaded_by_id.get(requester_id)
    requested_increment = budget if requester_loaded is False else 0
    projected = committed + requested_increment
    shortfall = max(0, projected - resource.allocatable_mb)

    base = dict(
        operation=operation,
        backend_id=requester_id,
        resource_id=resource_id,
        global_mode=config.gpu_arbiter_mode.value,
        desired_mode=desired.value,
        effective_mode=effective.value,
        allocatable_mb=resource.allocatable_mb,
        committed_before_mb=committed,
        requested_increment_mb=requested_increment,
        projected_mb=projected,
        shortfall_mb=shortfall,
        uncertain_backend_ids=tuple(sorted(uncertain)),
    )
    if shortfall == 0:
        reason = (
            "requester_already_or_conservatively_committed"
            if requested_increment == 0
            else "capacity_available"
        )
        return GPUShadowDecision(
            decision="would-admit",
            reason=reason,
            **base,
        )

    requester_priority = getattr(requester, "eviction_priority", 0)
    if isinstance(requester_priority, bool) or not isinstance(requester_priority, int):
        requester_priority = 0
    candidates = []
    for backend_id, backend in rows_by_id.items():
        if backend_id == requester_id or loaded_by_id.get(backend_id) is not True:
            continue
        candidate = _safe_shadow_candidate(
            backend,
            trusted_meta_by_id.get(backend_id, {}),
            resource_id=resource_id,
            requester_priority=requester_priority,
        )
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda item: (item.eviction_priority, item.backend_id))
    candidate_tuple = tuple(candidates)
    candidate_capacity = sum(item.vram_budget_mb for item in candidates)
    if candidate_capacity >= shortfall:
        return GPUShadowDecision(
            decision="would-evict",
            reason="eligible_candidate_capacity_sufficient",
            candidates=candidate_tuple,
            **base,
        )
    return GPUShadowDecision(
        decision="would-reject",
        reason="capacity_or_trusted_candidate_unavailable",
        candidates=candidate_tuple,
        **base,
    )
