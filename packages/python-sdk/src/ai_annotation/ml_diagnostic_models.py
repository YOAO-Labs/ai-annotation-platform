"""Typed admin diagnostics for global and environment-managed ML backends."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ai_annotation.storage_models import BucketSummary
from ai_annotation.models import GpuInfo, MLBackend, _AAPModel


class _RequestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        hide_input_in_errors=True,
        protected_namespaces=("model_validate", "model_dump"),
    )


class ProbeRequest(_RequestModel):
    url: str = Field(min_length=1, max_length=500)
    auth_method: Literal["none", "token"] = "none"
    auth_token: str | None = Field(default=None, max_length=500, repr=False)


class ProbeResponse(_AAPModel):
    ok: bool
    latency_ms: int
    status_code: int | None = None
    error: str | None = None
    gpu_info: GpuInfo | dict[str, Any] | None = None
    cache: dict[str, Any] | None = None
    model_version: str | None = None


class RuntimeHints(_AAPModel):
    ml_backend_default_url: str | None = None


class StorageOverview(_AAPModel):
    items: list[BucketSummary]
    total_object_count: int
    total_size_bytes: int


class ProjectMLBackendsGroup(_AAPModel):
    project_id: str
    project_name: str
    backends: list[MLBackend]


class MLIntegrationsOverview(_AAPModel):
    storage: StorageOverview
    projects: list[ProjectMLBackendsGroup]
    total_backends: int
    connected_backends: int


class GPUConfigDiagnostic(_AAPModel):
    code: str
    level: Literal["info", "warning", "critical", "blocker"]
    message: str
    field: str | None = None
    resource_id: str | None = None
    backend_id: UUID | None = None


GPUDesiredMode = Literal["off", "observe", "enforce"]
GPURolloutState = Literal[
    "disabled", "uninitialized", "off", "promoting", "enforcing", "demoting", "blocked"
]
GPUResourceStatus = Literal["ok", "info", "warning", "critical", "blocker"]


class GPUArbiterRuntimeObservation(_AAPModel):
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
    ledger_revision: int | None = None
    ledger_incarnation: str | None = None
    reconcile_deadline_ms: int | None = None
    committed_mb: int | None = None
    backend_count: int
    active_backend_count: int
    membership_state_counts: dict[str, int]
    allocation_state_counts: dict[str, int]
    lease_count: int | None
    card_queue_count: int | None
    backend_queue_count: int | None
    transition_present: bool | None
    durable_domain_matches: bool | None = None


class GPUArbiterRollout(_AAPModel):
    enabled: bool
    state: GPURolloutState
    effective_mode: GPUDesiredMode
    target_mode: GPUDesiredMode | None = None
    dispatch_blocked: bool
    blocked_reason: str | None = None
    transition_id: UUID | None = None
    last_transition_id: UUID | None = None
    revision: int | None = None


class GPUArbiterResource(_AAPModel):
    gpu_resource_id: str
    node_id: str
    physical_device_token: str
    allocatable_mb: int
    configured_mode: GPUDesiredMode | None = None
    desired_mode: GPUDesiredMode
    effective_mode: GPUDesiredMode
    claimed_budget_mb: int
    claimed_backend_count: int
    status: GPUResourceStatus
    diagnostics: list[GPUConfigDiagnostic] = Field(default_factory=list)
    rollout: GPUArbiterRollout
    runtime: GPUArbiterRuntimeObservation


class GPUArbiterResourcesResponse(_AAPModel):
    global_desired_mode: GPUDesiredMode
    rollout_enabled: bool
    runtime_ready: bool
    observe_runtime_ready: bool
    enforce_runtime_ready: bool
    resources: list[GPUArbiterResource]
    diagnostics: list[GPUConfigDiagnostic]


class VariantCatalog(_AAPModel):
    sam_variant: list[str] = Field(default_factory=list)
    dino_variant: list[str] = Field(default_factory=list)


class ObserveTarget(_AAPModel):
    url: str
    ok: bool
    latency_ms: int
    status_code: int | None = None
    error: str | None = None
    gpu_info: GpuInfo | dict[str, Any] | None = None
    model_version: str | None = None
    pool: dict[str, Any] | None = None
    video_pool: dict[str, Any] | None = None
    cache: dict[str, Any] | None = None
    compute: dict[str, Any] | None = None
    residency: dict[str, Any] | None = None
    variant_catalog: VariantCatalog | None = None
    supported_variants: list[dict[str, Any]] = Field(default_factory=list)
    supported_trackers: list[str] = Field(default_factory=list)
    supports_variants: bool = False
    registered: bool = False
    registered_label: str | None = None


class ObserveResponse(_AAPModel):
    targets: list[ObserveTarget]
    configured_count: int


class SmokeTestRequest(_RequestModel):
    url: str = Field(min_length=1, max_length=500)
    sam_variant: str | None = None
    dino_variant: str | None = None
    variant: dict[str, Any] | None = None


class SmokeTestResponse(_AAPModel):
    ok: bool
    skipped: bool = False
    reloaded: bool | None = None
    auto_unloaded: bool = False
    load_latency_ms: int | None = None
    loaded_variant: dict[str, Any] | None = None
    message: str
    error: str | None = None
