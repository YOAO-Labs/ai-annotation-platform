"""Closed private wire contract for GPU authority Native data actions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

GPU_AUTHORITY_PROTOCOL = "aap-inference-gpu-authority/v1"
GPU_AUTHORITY_DATA_PROTOCOL = "aap-inference-gpu-native/v1"
GPU_AUTHORITY_REQUEST_MAX_BYTES = 1024 * 1024
GPU_AUTHORITY_REPLY_MAX_BYTES = 4 * 1024 * 1024
GPU_AUTHORITY_NATIVE_SCHEMA = "aap_inference_gpu_v1"
GPU_AUTHORITY_SCHEMA = GPU_AUTHORITY_NATIVE_SCHEMA
GPU_COLLECTOR_SCHEMA = "aap_inference_gpu_collector_v1"
GPU_COLLECTOR_REQUEST_MAX_BYTES = 1024 * 1024
GPU_COLLECTOR_REPLY_MAX_BYTES = 4 * 1024 * 1024

GPU_DISPATCH_HTTP_PROTOCOL = "aap-inference-gpu-dispatch/v1"
GPU_DISPATCH_ACQUIRE_PATH = "/internal/inference/gpu/v1/dispatch/acquire"
GPU_DISPATCH_SETTLE_PATH = "/internal/inference/gpu/v1/dispatch/settle"
GPU_ADMIN_HTTP_PROTOCOL = "aap-inference-gpu-admin/v1"
GPU_ADMIN_ACTIVATE_PATH = "/internal/inference/gpu/v1/admin/activate-memberships"
GPU_ADMIN_ROLLOUT_PATH = "/internal/inference/gpu/v1/admin/rollout-control"
GPU_ADMIN_OBSERVE_PATH = "/internal/inference/gpu/v1/admin/observe-resource"
GPU_ADMIN_REPAIR_PATH = "/internal/inference/gpu/v1/admin/repair-resource"
GPU_ADMIN_PROBE_PATH = "/internal/inference/gpu/v1/admin/probe-retired-membership"
GPU_ADMIN_COLLECT_PATH = "/internal/inference/gpu/v1/admin/collect-tombstone"
GPU_ADMIN_DIAGNOSTICS_PATH = "/internal/inference/gpu/v1/admin/diagnostics"
GPU_ADMIN_INSTALLATION_ACTIVATE_PATH = "/internal/inference/gpu/v1/admin/activate-installation"
GPU_ADMIN_INSTALLATION_QUIESCE_PATH = "/internal/inference/gpu/v1/admin/quiesce-installation"
GPU_AUTHORITY_NATIVE_FUNCTIONS = (
    "aap_inference_gpu_v1.gpu_authority_policy()",
    "aap_inference_gpu_v1.gpu_authority_step(text,text)",
)
GPU_AUTHORITY_NATIVE_ACTIONS = (
    "fence_advance",
    "fence_token_expiry",
    "fence_activate_membership",
    "fence_read",
    "rollout_read",
    "rollout_read_all",
    "rollout_boundary",
    "rollout_begin",
    "rollout_complete",
    "rollout_block",
    "runtime_subject",
    "cold_runtime_subject",
    "idle_eviction_subject",
    "busy_eviction_subject",
    "eviction_drain_health",
    "prepare_cold_generation",
    "prepare_idle_eviction_generation",
    "prepare_eviction_cancel_generation",
    "record_runtime_token_expiry",
    "cold_terminal_from_health",
    "eviction_cancel_from_health",
    "eviction_phase_from_health",
    "proof_domain",
    "proof_reset_from_health",
    "reconcile_resource",
    "repair_resource",
    "prepare_legacy_ack",
    "prepare_rollout_control",
    "membership_candidates",
    "membership_max_concurrency",
    "retired_probe_subject",
    "retired_probe_commit",
    "shadow_snapshot",
    "resource_runtime_observation",
    "installation_terminal",
    "installation_control",
)

GPU_CALLER_DATA_PROTOCOL = "aap-inference-gpu-caller-native/v1"
GPU_CALLER_NATIVE_SCHEMA = "aap_inference_gpu_caller_v1"
GPU_CALLER_NATIVE_FUNCTIONS = (
    "aap_inference_gpu_caller_v1.gpu_caller_policy()",
    "aap_inference_gpu_caller_v1.gpu_caller_step(text,text)",
)
GPU_CALLER_NATIVE_ACTIONS = (
    "fence_advance",
    "fence_token_expiry",
    "fence_activate_membership",
    "fence_read",
    "rollout_read",
    "rollout_read_all",
    "rollout_boundary",
    "rollout_begin",
    "rollout_complete",
    "rollout_block",
    "prepare_legacy_ack",
    "prepare_rollout_control",
    "membership_candidates",
    "membership_max_concurrency",
)
GPU_CALLER_REQUEST_MAX_BYTES = GPU_AUTHORITY_REQUEST_MAX_BYTES
GPU_CALLER_REPLY_MAX_BYTES = GPU_AUTHORITY_REPLY_MAX_BYTES

GPU_COLLECTOR_DATA_PROTOCOL = "aap-inference-gpu-collector-native/v1"
GPU_COLLECTOR_NATIVE_SCHEMA = "aap_inference_gpu_collector_v1"
GPU_COLLECTOR_NATIVE_FUNCTIONS = (
    "aap_inference_gpu_collector_v1.gpu_collector_policy()",
    "aap_inference_gpu_collector_v1.gpu_collector_step(text,text)",
)
GPU_COLLECTOR_NATIVE_ACTIONS = ("collect_tombstone",)

GPUAuthorityAction = Literal[
    "fence_advance",
    "fence_token_expiry",
    "fence_activate_membership",
    "fence_read",
    "rollout_read",
    "rollout_read_all",
    "rollout_boundary",
    "rollout_begin",
    "rollout_complete",
    "rollout_block",
    "runtime_subject",
    "cold_runtime_subject",
    "idle_eviction_subject",
    "busy_eviction_subject",
    "eviction_drain_health",
    "prepare_cold_generation",
    "prepare_idle_eviction_generation",
    "prepare_eviction_cancel_generation",
    "record_runtime_token_expiry",
    "cold_terminal_from_health",
    "eviction_cancel_from_health",
    "eviction_phase_from_health",
    "proof_domain",
    "proof_reset_from_health",
    "reconcile_resource",
    "repair_resource",
    "prepare_legacy_ack",
    "prepare_rollout_control",
    "membership_candidates",
    "membership_max_concurrency",
    "retired_probe_subject",
    "retired_probe_commit",
    "shadow_snapshot",
    "resource_runtime_observation",
    "installation_terminal",
    "installation_control",
]

GPUCallerAction = Literal[
    "fence_advance",
    "fence_token_expiry",
    "fence_activate_membership",
    "fence_read",
    "rollout_read",
    "rollout_read_all",
    "rollout_boundary",
    "rollout_begin",
    "rollout_complete",
    "rollout_block",
    "prepare_legacy_ack",
    "prepare_rollout_control",
    "membership_candidates",
    "membership_max_concurrency",
]


class GPUAuthorityUnavailable(Exception):
    """GPU authority is unavailable or returned malformed Native data."""

    def __init__(self) -> None:
        super().__init__("GPU authority unavailable")


class _GPUModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class GPUDispatchAcquireRequest(_GPUModel):
    request_id: UUID
    protocol: Literal["aap-inference-gpu-dispatch/v1"] = GPU_DISPATCH_HTTP_PROTOCOL
    backend_id: str = Field(min_length=1, max_length=128)
    gpu_resource_id: str = Field(min_length=1, max_length=256)
    operation: Literal[
        "predict",
        "predict_interactive",
        "warmup",
        "reload",
        "unload",
    ]
    scope: Literal["predict", "warmup", "reload"]


class GPUDispatchAcquireReply(_GPUModel):
    protocol: Literal["aap-inference-gpu-dispatch/v1"] = GPU_DISPATCH_HTTP_PROTOCOL
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    generation: str = Field(pattern=r"^[1-9][0-9]*$")
    admission_token: str = Field(min_length=1, max_length=16384)


class GPUDispatchSettleRequest(_GPUModel):
    protocol: Literal["aap-inference-gpu-dispatch/v1"] = GPU_DISPATCH_HTTP_PROTOCOL
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    outcome: Literal["response_received", "not_sent", "uncertain"]
    status_code: int | None = Field(default=None, ge=100, le=599)
    reason: Literal["request_aborted", "response_not_reported"] | None = None


class GPUDispatchSettleReply(_GPUModel):
    protocol: Literal["aap-inference-gpu-dispatch/v1"] = GPU_DISPATCH_HTTP_PROTOCOL
    settled: bool


class GPUInstallationControlRequest(_GPUModel):
    protocol: Literal["aap-inference-gpu-admin/v1"] = GPU_ADMIN_HTTP_PROTOCOL
    registry_id: UUID
    resource_id: str = Field(min_length=1, max_length=256)
    endpoint: str = Field(min_length=1, max_length=2048)
    boot_id: str = Field(min_length=1, max_length=128)
    operation_id: UUID
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    pool_ids: list[str] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def valid_pool_ids(self):
        if any(not pool or len(pool) > 128 or pool.strip() != pool for pool in self.pool_ids):
            raise ValueError("invalid GPU installation pool IDs")
        if self.pool_ids != sorted(set(self.pool_ids)):
            raise ValueError("GPU installation pool IDs must be unique and ordered")
        return self


class GPUInstallationActivationReply(_GPUModel):
    protocol: Literal["aap-inference-gpu-admin/v1"] = GPU_ADMIN_HTTP_PROTOCOL
    registry_id: UUID
    resource_id: str = Field(min_length=1, max_length=256)
    endpoint: str = Field(min_length=1, max_length=2048)
    boot_id: str = Field(min_length=1, max_length=128)
    control_epoch: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    pool_ids: list[str] = Field(min_length=1, max_length=128)


class GPUInstallationQuiesceReply(GPUInstallationActivationReply):
    generation: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    operation_id: UUID
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    unloaded: Literal[True]


class GPUActivateMembershipsRequest(_GPUModel):
    protocol: Literal["aap-inference-gpu-admin/v1"] = GPU_ADMIN_HTTP_PROTOCOL
    resource_id: str = Field(min_length=1, max_length=256)
    pending_only: bool = False


class GPUActivateMembershipResult(_GPUModel):
    backend_id: UUID
    resource_id: str = Field(min_length=1, max_length=256)
    membership_epoch: int = Field(ge=1)
    status: Literal[
        "promoted", "acknowledged", "blocked", "active_unacked", "unavailable"
    ]
    reason: str = Field(min_length=1, max_length=256)
    runtime_epoch: str | None = None
    control_epoch: str | None = None
    requires_proof_reset: bool = True


class GPUActivateMembershipsReply(_GPUModel):
    protocol: Literal["aap-inference-gpu-admin/v1"] = GPU_ADMIN_HTTP_PROTOCOL
    results: list[GPUActivateMembershipResult]


class GPURolloutControlRequest(_GPUModel):
    protocol: Literal["aap-inference-gpu-admin/v1"] = GPU_ADMIN_HTTP_PROTOCOL
    resource_id: str = Field(min_length=1, max_length=256)
    transition_id: UUID
    target_gate: Literal["legacy", "enforce"]


class GPURolloutControlResult(_GPUModel):
    backend_id: UUID
    resource_id: str = Field(min_length=1, max_length=256)
    membership_epoch: int = Field(ge=1)
    transition_id: UUID
    operation: Literal["reset", "mode_enforce", "mode_legacy"] | None
    status: Literal["acknowledged", "issued", "pending", "blocked", "unavailable"]
    reason: str = Field(min_length=1, max_length=256)
    control_epoch: str | None = None


class GPURolloutControlReply(_GPUModel):
    protocol: Literal["aap-inference-gpu-admin/v1"] = GPU_ADMIN_HTTP_PROTOCOL
    results: list[GPURolloutControlResult]


class GPUResourceObservationRequest(_GPUModel):
    protocol: Literal["aap-inference-gpu-admin/v1"] = GPU_ADMIN_HTTP_PROTOCOL
    resource_id: str = Field(min_length=1, max_length=256)
    allocatable_mb: int | None = Field(default=None, ge=1)


class GPUResourceRuntimeObservationReply(_GPUModel):
    protocol: Literal["aap-inference-gpu-admin/v1"] = GPU_ADMIN_HTTP_PROTOCOL
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
    backend_count: int = Field(ge=0)
    active_backend_count: int = Field(ge=0)
    membership_state_counts: dict[str, int]
    allocation_state_counts: dict[str, int]
    lease_count: int | None
    card_queue_count: int | None
    backend_queue_count: int | None
    transition_present: bool | None
    durable_domain_matches: bool | None


class GPUAdminMembershipCandidate(_GPUModel):
    backend_id: UUID
    membership_epoch: int = Field(ge=1)


class GPUAdminRolloutSnapshot(_GPUModel):
    resource_id: str = Field(min_length=1, max_length=512)
    state: Literal["off", "promoting", "enforcing", "demoting", "blocked"]
    effective_mode: Literal["off", "enforce"]
    target_mode: Literal["off", "observe", "enforce"]
    transition_id: UUID | None
    last_transition_id: UUID | None
    blocker_reason: str | None
    revision: int = Field(ge=1)


class GPUAdminFenceSnapshot(_GPUModel):
    backend_registry_id: UUID
    generation_high_water: int = Field(ge=0)
    control_epoch_high_water: int = Field(ge=0)
    runtime_epoch_high_water: int = Field(ge=0)
    token_expiry_high_water: str | None
    updated_at: str


class GPUResourceRepairRequest(_GPUModel):
    protocol: Literal["aap-inference-gpu-admin/v1"] = GPU_ADMIN_HTTP_PROTOCOL
    resource_id: str = Field(min_length=1, max_length=256)
    allocatable_mb: int | None = Field(default=None, ge=1)
    force_proof_reset: bool = False
    readiness_blocker: str | None = Field(default=None, max_length=256)


class GPUResourceRepairReply(_GPUModel):
    protocol: Literal["aap-inference-gpu-admin/v1"] = GPU_ADMIN_HTTP_PROTOCOL
    resource_id: str = Field(min_length=1, max_length=256)
    action: Literal["already_ready", "bootstrap", "repair", "resume_prepared"]
    status: str
    ready: bool
    reason: str
    ledger_revision: int = Field(ge=0)
    ledger_incarnation: str = Field(min_length=1)
    committed_mb: int = Field(ge=0)


class GPURetiredMembershipRequest(_GPUModel):
    protocol: Literal["aap-inference-gpu-admin/v1"] = GPU_ADMIN_HTTP_PROTOCOL
    backend_id: UUID
    resource_id: str = Field(min_length=1, max_length=256)
    membership_epoch: int = Field(ge=1)


class GPURetiredMembershipReply(_GPUModel):
    protocol: Literal["aap-inference-gpu-admin/v1"] = GPU_ADMIN_HTTP_PROTOCOL
    backend_id: UUID
    resource_id: str = Field(min_length=1, max_length=256)
    membership_epoch: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=256)
    proof_available: bool


class GPUTombstoneCollectionReply(_GPUModel):
    protocol: Literal["aap-inference-gpu-admin/v1"] = GPU_ADMIN_HTTP_PROTOCOL
    backend_id: UUID
    resource_id: str = Field(min_length=1, max_length=256)
    membership_epoch: int = Field(ge=1)
    status: Literal["collected", "blocked", "stale", "error"]
    reason: str = Field(min_length=1, max_length=256)
    idempotent: bool = False


class GPUAdminDiagnosticsRequest(_GPUModel):
    protocol: Literal["aap-inference-gpu-admin/v1"] = GPU_ADMIN_HTTP_PROTOCOL
    resource_id: str | None = Field(default=None, min_length=1, max_length=256)


class GPUAdminDiagnosticsReply(_GPUModel):
    protocol: Literal["aap-inference-gpu-admin/v1"] = GPU_ADMIN_HTTP_PROTOCOL
    result: dict[str, Any]


class GPUAuthorityErrorReply(_GPUModel):
    status_code: int = Field(ge=100, le=599)
    detail: dict[str, Any]
    headers: dict[str, str] = Field(default_factory=dict)
    error_code: str = Field(min_length=1)
    retry_after_s: int | None = Field(default=None, ge=0)


class GPUAuthorityRequest(_GPUModel):
    protocol: Literal["aap-inference-gpu-authority/v1"] = GPU_AUTHORITY_PROTOCOL
    action: GPUAuthorityAction
    arguments: dict[str, Any] = Field(default_factory=dict)


class GPUAuthorityReply(_GPUModel):
    protocol: Literal["aap-inference-gpu-authority/v1"] = GPU_AUTHORITY_PROTOCOL
    action: GPUAuthorityAction
    result: dict[str, Any]


class GPUCollectorRequest(_GPUModel):
    protocol: Literal["aap-inference-gpu-collector/v1"] = (
        "aap-inference-gpu-collector/v1"
    )
    action: Literal["collect_tombstone"] = "collect_tombstone"
    arguments: dict[str, Any] = Field(default_factory=dict)


class GPUCollectorReply(_GPUModel):
    protocol: Literal["aap-inference-gpu-collector/v1"] = (
        "aap-inference-gpu-collector/v1"
    )
    action: Literal["collect_tombstone"]
    result: dict[str, Any]


def gpu_dispatch_settle_request_valid(value: GPUDispatchSettleRequest) -> bool:
    if value.outcome == "response_received":
        return value.status_code is not None and value.reason is None
    if value.outcome == "uncertain":
        return value.status_code is None and value.reason is not None
    return value.status_code is None and value.reason is None


def gpu_authority_native_policy_ready(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        value.get("protocol") == GPU_AUTHORITY_DATA_PROTOCOL
        and value.get("native_schema") == GPU_AUTHORITY_NATIVE_SCHEMA
        and tuple(value.get("functions", ())) == GPU_AUTHORITY_NATIVE_FUNCTIONS
        and tuple(value.get("actions", ())) == GPU_AUTHORITY_NATIVE_ACTIONS
        and value.get("ready") is True
    )


def gpu_caller_native_policy_ready(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        value.get("protocol") == GPU_CALLER_DATA_PROTOCOL
        and value.get("native_schema") == GPU_CALLER_NATIVE_SCHEMA
        and tuple(value.get("functions", ())) == GPU_CALLER_NATIVE_FUNCTIONS
        and tuple(value.get("actions", ())) == GPU_CALLER_NATIVE_ACTIONS
        and value.get("ready") is True
    )


def gpu_collector_native_policy_ready(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        value.get("protocol") == GPU_COLLECTOR_DATA_PROTOCOL
        and value.get("native_schema") == GPU_COLLECTOR_NATIVE_SCHEMA
        and tuple(value.get("functions", ())) == GPU_COLLECTOR_NATIVE_FUNCTIONS
        and tuple(value.get("actions", ())) == GPU_COLLECTOR_NATIVE_ACTIONS
        and value.get("ready") is True
    )


__all__ = [
    "GPU_AUTHORITY_DATA_PROTOCOL",
    "GPU_AUTHORITY_NATIVE_ACTIONS",
    "GPU_AUTHORITY_NATIVE_FUNCTIONS",
    "GPU_AUTHORITY_NATIVE_SCHEMA",
    "GPU_AUTHORITY_PROTOCOL",
    "GPU_CALLER_DATA_PROTOCOL",
    "GPU_CALLER_NATIVE_ACTIONS",
    "GPU_CALLER_NATIVE_FUNCTIONS",
    "GPU_CALLER_NATIVE_SCHEMA",
    "GPU_COLLECTOR_DATA_PROTOCOL",
    "GPU_COLLECTOR_NATIVE_ACTIONS",
    "GPU_COLLECTOR_NATIVE_FUNCTIONS",
    "GPU_ADMIN_ACTIVATE_PATH",
    "GPU_ADMIN_COLLECT_PATH",
    "GPU_ADMIN_DIAGNOSTICS_PATH",
    "GPU_ADMIN_HTTP_PROTOCOL",
    "GPU_ADMIN_INSTALLATION_ACTIVATE_PATH",
    "GPU_ADMIN_INSTALLATION_QUIESCE_PATH",
    "GPU_ADMIN_OBSERVE_PATH",
    "GPU_ADMIN_PROBE_PATH",
    "GPU_ADMIN_REPAIR_PATH",
    "GPU_ADMIN_ROLLOUT_PATH",
    "GPU_DISPATCH_ACQUIRE_PATH",
    "GPU_DISPATCH_HTTP_PROTOCOL",
    "GPU_DISPATCH_SETTLE_PATH",
    "GPUActivateMembershipResult",
    "GPUActivateMembershipsReply",
    "GPUActivateMembershipsRequest",
    "GPUAdminDiagnosticsRequest",
    "GPUAdminFenceSnapshot",
    "GPUAdminMembershipCandidate",
    "GPUAdminRolloutSnapshot",
    "GPUAdminDiagnosticsReply",
    "GPUAuthorityAction",
    "GPUAuthorityErrorReply",
    "GPUAuthorityReply",
    "GPUAuthorityRequest",
    "GPUAuthorityUnavailable",
    "GPUCollectorReply",
    "GPUCollectorRequest",
    "GPUCallerAction",
    "GPUDispatchAcquireReply",
    "GPUDispatchAcquireRequest",
    "GPUDispatchSettleReply",
    "GPUDispatchSettleRequest",
    "GPUInstallationActivationReply",
    "GPUInstallationControlRequest",
    "GPUInstallationQuiesceReply",
    "GPUResourceObservationRequest",
    "GPUResourceRepairReply",
    "GPUResourceRepairRequest",
    "GPUResourceRuntimeObservationReply",
    "GPURetiredMembershipReply",
    "GPURetiredMembershipRequest",
    "GPURolloutControlReply",
    "GPURolloutControlRequest",
    "GPURolloutControlResult",
    "GPUTombstoneCollectionReply",
    "gpu_authority_native_policy_ready",
    "gpu_collector_native_policy_ready",
    "gpu_dispatch_settle_request_valid",
    "gpu_caller_native_policy_ready",
]
