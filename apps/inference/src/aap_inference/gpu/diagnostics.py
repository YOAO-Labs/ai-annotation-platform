"""Framework-neutral GPU configuration and resource diagnostics.

Backend status is computed from immutable owner facts, never ORM instances. The
caller retains ownership of any API-specific logging or database work.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

from aap_inference.gpu.contracts import GPUBackendConfigStatus, GPUConfigDiagnostic
from aap_inference.gpu.policy import (
    _as_mapping,
    _claim_shape_diagnostics,
    _diag,
    _is_explicit_cpu_backend,
    _requires_gpu_claim,
    _trusted_health_meta,
    effective_gpu_arbiter_mode,
)
from aap_inference.gpu.rollout_state import (
    GPUArbiterRolloutDecision,
    GPUArbiterRolloutSnapshot,
)
from aap_inference.gpu.settings import (
    GPUArbiterMode,
    GPUAuthoritySettings as Settings,
)


@dataclass(frozen=True)
class GPUBackendDiagnosticFacts:
    """Credential-free backend facts required by GPU config diagnostics."""

    backend_id: UUID
    gpu_resource_id: str | None
    vram_budget_mb: int | None
    state: str
    last_checked_at: datetime | None
    health_meta: Mapping[str, Any] | None
    retired_installation: bool = False


logger = structlog.get_logger(__name__)

_LEVEL_ORDER = {
    "ok": 0,
    "info": 1,
    "warning": 2,
    "critical": 3,
    "blocker": 4,
}



def claimed_budget_by_resource(
    backends: Iterable[GPUBackendDiagnosticFacts],
) -> dict[str, int]:
    totals: dict[str, int] = defaultdict(int)
    for backend in backends:
        resource_id = backend.gpu_resource_id
        budget = backend.vram_budget_mb
        if (
            resource_id is not None
            and isinstance(budget, int)
            and not isinstance(budget, bool)
        ):
            totals[resource_id] += budget
    return dict(totals)


def record_unregistered_gpu_shadow_dispatch(
    url: str, operation: str, *, config: Settings
) -> None:
    """Expose a smoke-test bypass without pretending it has a managed claim."""

    if config.gpu_arbiter_mode is not GPUArbiterMode.OBSERVE:
        return
    if operation == "unload":
        logger.warning(
            "gpu_arbiter_shadow_unregistered_unload",
            gpu_arbiter={
                "reason": "unmanaged_unregistered_target",
                "operation": operation,
                "url": url,
                "resource_id": None,
                "authoritative": False,
                "releases_allocation": False,
                "business_request_blocked": False,
            },
        )
        return
    logger.warning(
        "gpu_arbiter_shadow_unregistered_dispatch",
        gpu_arbiter={
            "decision": "would-reject",
            "reason": "unmanaged_unregistered_target",
            "operation": operation,
            "url": url,
            "resource_id": None,
            "authoritative": False,
            "business_request_blocked": False,
        },
    )


def _identity_diagnostic(
    backend: GPUBackendDiagnosticFacts,
    physical_token: str,
    health_meta: Mapping[str, Any],
) -> GPUConfigDiagnostic | None:
    gpu_info = _as_mapping(health_meta.get("gpu_info"))
    observed: str | int | None = None
    expected: str | int = physical_token
    if physical_token.startswith("MIG-"):
        observed = gpu_info.get("mig_uuid") or gpu_info.get("physical_device_token")
    elif physical_token.startswith("GPU-"):
        observed = gpu_info.get("device_uuid") or gpu_info.get("physical_device_token")
    elif physical_token.startswith("index:"):
        try:
            expected = int(physical_token.removeprefix("index:"))
        except ValueError:
            return None
        observed = gpu_info.get("device_index")
    if observed is None:
        return _diag(
            "gpu_identity_unverified",
            "warning",
            f"backend 尚未上报可与 {physical_token} 对账的物理设备标识",
            field="gpu_resource_id",
            resource_id=backend.gpu_resource_id,
            backend_id=backend.backend_id,
        )
    if observed == expected:
        return None
    return _diag(
        "gpu_identity_mismatch",
        "blocker",
        f"观测到的物理设备 {observed} 与声明 {physical_token} 不一致",
        field="gpu_resource_id",
        resource_id=backend.gpu_resource_id,
        backend_id=backend.backend_id,
    )


def build_backend_gpu_config_status(
    backend: GPUBackendDiagnosticFacts,
    totals: Mapping[str, int],
    *,
    config: Settings,
) -> GPUBackendConfigStatus:
    resource_id = backend.gpu_resource_id
    budget = backend.vram_budget_mb
    backend_id = backend.backend_id
    diagnostics = _claim_shape_diagnostics(resource_id, budget, backend_id=backend_id)
    allocatable: int | None = None
    desired_mode = config.gpu_arbiter_desired_mode(resource_id or "").value
    effective_mode = (
        effective_gpu_arbiter_mode(resource_id, config=config).value
        if resource_id
        else GPUArbiterMode.OFF.value
    )
    health_meta = _trusted_health_meta(backend)
    requires_gpu_claim = _requires_gpu_claim(health_meta)

    if resource_id is None and budget is None and backend.retired_installation:
        pass
    elif resource_id is None and budget is None:
        if requires_gpu_claim:
            diagnostics.append(
                _diag(
                    "gpu_claim_missing",
                    "blocker",
                    "backend 配置使用 GPU，但尚未声明物理资源与显存预算",
                    field="gpu_resource_id",
                    backend_id=backend_id,
                )
            )
        elif _is_explicit_cpu_backend(
            health_meta, requires_gpu_claim=requires_gpu_claim
        ):
            diagnostics.append(
                _diag(
                    "explicit_cpu_backend",
                    "info",
                    "backend 显式配置为 CPU，无需声明 GPU 资源",
                    field="gpu_resource_id",
                    backend_id=backend_id,
                )
            )
        else:
            diagnostics.append(
                _diag(
                    "gpu_claim_unknown",
                    "blocker",
                    "backend 未显式证明为 CPU，必须声明 GPU 资源或先完成设备探测",
                    field="gpu_resource_id",
                    backend_id=backend_id,
                )
            )
    elif resource_id is not None and not diagnostics:
        resource = config.gpu_arbiter_resources.get(resource_id)
        if config.gpu_arbiter_config_errors:
            diagnostics.append(
                _diag(
                    "gpu_resources_config_invalid",
                    "blocker",
                    "GPU_ARBITER_RESOURCES_JSON 无法解析："
                    f"{config.gpu_arbiter_config_errors[0]}",
                    field="gpu_resource_id",
                    resource_id=resource_id,
                    backend_id=backend_id,
                )
            )
        elif resource is None:
            diagnostics.append(
                _diag(
                    "gpu_resource_unknown",
                    "blocker",
                    f"未在 GPU_ARBITER_RESOURCES_JSON 中找到 {resource_id}",
                    field="gpu_resource_id",
                    resource_id=resource_id,
                    backend_id=backend_id,
                )
            )
        else:
            allocatable = resource.allocatable_mb
            if budget is not None and budget > allocatable:
                diagnostics.append(
                    _diag(
                        "vram_budget_exceeds_allocatable",
                        "blocker",
                        f"预算 {budget} MiB 超过该资源可分配容量 {allocatable} MiB",
                        field="vram_budget_mb",
                        resource_id=resource_id,
                        backend_id=backend_id,
                    )
                )
            elif totals.get(resource_id, 0) > allocatable:
                diagnostics.append(
                    _diag(
                        "gpu_resource_oversubscribed",
                        "warning",
                        f"同卡静态预算合计 {totals[resource_id]} MiB 超过可分配容量 "
                        f"{allocatable} MiB；这是允许驱逐的弹性超售",
                        resource_id=resource_id,
                        backend_id=backend_id,
                    )
                )
            identity = _identity_diagnostic(
                backend, resource.physical_device_token, health_meta
            )
            if identity is not None:
                diagnostics.append(identity)
            if desired_mode == "enforce":
                diagnostics.append(
                    _diag(
                        "gpu_arbiter_runtime_not_ready",
                        "blocker",
                        "GPU 仲裁期望模式为 enforce，但账本与 gate 握手尚未就绪；"
                        "实际模式保持 off",
                        resource_id=resource_id,
                        backend_id=backend_id,
                    )
                )

    status = "ok"
    for diagnostic in diagnostics:
        if _LEVEL_ORDER[diagnostic.level] > _LEVEL_ORDER[status]:
            status = diagnostic.level
    return GPUBackendConfigStatus(
        status=status,
        desired_mode=desired_mode,
        effective_mode=effective_mode,
        allocatable_mb=allocatable,
        resource_claimed_budget_mb=totals.get(resource_id) if resource_id else None,
        diagnostics=diagnostics,
    )


def apply_backend_rollout_status(
    status: GPUBackendConfigStatus,
    resource_id: str,
    decision: GPUArbiterRolloutDecision,
    rollout: GPUArbiterRolloutSnapshot | None,
    *,
    config: Settings,
    backend_id: UUID | None,
) -> GPUBackendConfigStatus:
    """Project rollout dispatch readiness into a backend's config status."""

    status.diagnostics = [
        item
        for item in status.diagnostics
        if item.code != "gpu_arbiter_runtime_not_ready"
    ]
    desired = config.gpu_arbiter_desired_mode(resource_id)
    diagnostic: GPUConfigDiagnostic | None = None
    if not config.gpu_arbiter_rollout_enabled:
        if rollout is not None and rollout.state != "off":
            diagnostic = _diag(
                "gpu_rollout_active_while_disabled",
                "blocker",
                f"持久 rollout 仍为 {rollout.state}，不能直接关闭 "
                "GPU_ARBITER_ROLLOUT_ENABLED；请先完成安全 demotion",
                resource_id=resource_id,
                backend_id=backend_id,
            )
        elif desired is GPUArbiterMode.ENFORCE:
            diagnostic = _diag(
                "gpu_rollout_disabled",
                "blocker",
                "GPU rollout 发布闩未开启，effective mode 保持 off",
                resource_id=resource_id,
                backend_id=backend_id,
            )
    elif decision.dispatch_blocked:
        diagnostic = _diag(
            "gpu_rollout_not_ready",
            "blocker",
            f"GPU rollout 尚未就绪：{decision.blocked_reason or decision.state}",
            resource_id=resource_id,
            backend_id=backend_id,
        )
    if diagnostic is not None:
        status.diagnostics.append(diagnostic)
    status.effective_mode = decision.effective_mode.value
    status.rollout_enabled = config.gpu_arbiter_rollout_enabled
    status.rollout_state = rollout.state if rollout is not None else decision.state
    status.rollout_revision = (
        rollout.revision if rollout is not None else decision.revision
    )
    status.rollout_blocker_reason = decision.blocked_reason or (
        rollout.blocker_reason if rollout is not None else None
    )
    status.status = max(
        (item.level for item in status.diagnostics),
        key=_LEVEL_ORDER.__getitem__,
        default="ok",
    )
    return status


def build_resource_summaries(
    backends: Iterable[GPUBackendDiagnosticFacts], *, config: Settings
) -> tuple[list[dict[str, Any]], list[GPUConfigDiagnostic]]:
    backend_rows = list(backends)
    totals = claimed_budget_by_resource(backend_rows)
    claim_counts: dict[str, int] = defaultdict(int)
    for backend in backend_rows:
        resource_id = backend.gpu_resource_id
        if resource_id:
            claim_counts[resource_id] += 1

    summaries: list[dict[str, Any]] = []
    diagnostics: list[GPUConfigDiagnostic] = [
        _diag(
            "gpu_resources_config_invalid",
            "blocker",
            f"GPU_ARBITER_RESOURCES_JSON 无法解析：{error}",
            field="gpu_arbiter_resources_json",
        )
        for error in config.gpu_arbiter_config_errors
    ]
    for resource_id, resource in sorted(config.gpu_arbiter_resources.items()):
        resource_diagnostics: list[GPUConfigDiagnostic] = []
        total = totals.get(resource_id, 0)
        if total > resource.allocatable_mb:
            resource_diagnostics.append(
                _diag(
                    "gpu_resource_oversubscribed",
                    "warning",
                    f"静态预算合计 {total} MiB 超过可分配容量 "
                    f"{resource.allocatable_mb} MiB",
                    resource_id=resource_id,
                )
            )
        if config.gpu_arbiter_mode.value != "off" and resource.mode is None:
            resource_diagnostics.append(
                _diag(
                    "gpu_resource_mode_missing",
                    "info",
                    "资源未显式声明 mode，有效模式安全保持 off",
                    resource_id=resource_id,
                )
            )
        desired_mode = config.gpu_arbiter_desired_mode(resource_id).value
        effective_mode = effective_gpu_arbiter_mode(resource_id, config=config).value
        if desired_mode == "enforce":
            resource_diagnostics.append(
                _diag(
                    "gpu_arbiter_runtime_not_ready",
                    "blocker",
                    "GPU 仲裁期望模式为 enforce，但账本与 gate 握手尚未就绪；"
                    "实际模式保持 off",
                    resource_id=resource_id,
                )
            )
        status = "ok"
        for diagnostic in resource_diagnostics:
            if _LEVEL_ORDER[diagnostic.level] > _LEVEL_ORDER[status]:
                status = diagnostic.level
        diagnostics.extend(resource_diagnostics)
        summaries.append(
            {
                "gpu_resource_id": resource_id,
                "node_id": resource.node_id,
                "physical_device_token": resource.physical_device_token,
                "allocatable_mb": resource.allocatable_mb,
                "configured_mode": resource.mode.value if resource.mode else None,
                "desired_mode": desired_mode,
                "effective_mode": effective_mode,
                "claimed_budget_mb": total,
                "claimed_backend_count": claim_counts.get(resource_id, 0),
                "status": status,
                "diagnostics": resource_diagnostics,
            }
        )

    for backend in backend_rows:
        status = build_backend_gpu_config_status(backend, totals, config=config)
        diagnostics.extend(
            diagnostic
            for diagnostic in status.diagnostics
            if diagnostic.code != "gpu_resource_oversubscribed"
        )
    return summaries, diagnostics
