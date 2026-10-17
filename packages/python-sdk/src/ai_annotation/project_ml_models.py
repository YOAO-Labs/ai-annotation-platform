"""Typed request and response contracts for project ML backend operations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    field_validator,
)

from ai_annotation.models import MLBackend, _AAPModel

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
_MAX_INTERACTIVE_CONTEXT_BYTES = 1024 * 1024


class _RequestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        hide_input_in_errors=True,
        protected_namespaces=("model_validate", "model_dump"),
    )


def _validate_backend_url(value: str | None) -> str | None:
    if value is None:
        return None
    if (urlparse(value).hostname or "").lower() in _LOOPBACK_HOSTS:
        raise ValueError("backend URL cannot use a loopback host")
    return value


class ProjectMLBackendCreateRequest(_RequestModel):
    name: str
    url: str
    is_interactive: bool = False
    auth_method: str = "none"
    auth_token: str | None = Field(default=None, repr=False)
    extra_params: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def _no_loopback(cls, value: str) -> str:
        return _validate_backend_url(value) or value


class ProjectMLBackendUpdateRequest(_RequestModel):
    name: str | None = None
    url: str | None = None
    is_interactive: bool | None = None
    auth_method: str | None = None
    auth_token: str | None = Field(default=None, repr=False)
    extra_params: dict[str, JsonValue] | None = None

    @field_validator("url")
    @classmethod
    def _no_loopback(cls, value: str | None) -> str | None:
        return _validate_backend_url(value)


class ProjectMLBackendReloadRequest(_RequestModel):
    sam_variant: str | None = None
    dino_variant: str | None = None
    modality: Literal["image", "video"] | None = None


class ProjectMLInteractiveRequest(_RequestModel):
    task_id: UUID
    context: dict[str, JsonValue] = Field(default_factory=dict)


class InteractiveRouting(_AAPModel):
    requested_backend_id: UUID
    backend_pool_id: UUID | None = None
    backend_instance_id: UUID | None = None
    model_id: str | None = None


class InteractiveDiagnostic(_AAPModel):
    reason: str
    retryable: bool = False
    message: str | None = None
    supported_geometric_outputs: list[str] | None = None


class ProjectMLInteractiveResponse(_AAPModel):
    result: list[dict[str, Any]] = Field(default_factory=list)
    score: float | None = None
    model_version: str | None = None
    inference_time_ms: float | None = None
    cache_hit: bool | None = None
    model_load_ms: float | None = None
    mask_input_next: str | None = None
    diagnostic: InteractiveDiagnostic | None = None
    prompt_revision: str | None = None
    output_geometry: Literal["polygon", "mask"] = "polygon"
    frame_index: int | None = None
    routing: InteractiveRouting
    prompt_summary: dict[str, Any] | None = None
    accept_receipts: dict[str, str] = Field(default_factory=dict)


class ProjectMLCapabilityWarning(_AAPModel):
    level: str = "warning"
    model_id: str | None = None
    field: str
    value: str
    message: str


class ProjectMLModelCapability(_AAPModel):
    id: str
    display_name: str | None = None
    task: str = "unknown"
    model_family: str | None = None
    infra: str = "unknown"
    is_interactive: bool = False
    supported_prompts: list[str] = Field(default_factory=list)
    supported_inputs: list[str] = Field(default_factory=list)
    default_input_type: str | None = None
    supported_geometric_outputs: list[str] = Field(default_factory=list)
    output_attribute_types: list[str] = Field(default_factory=list)
    output_attribute_schema: list[dict[str, Any]] = Field(default_factory=list)
    supported_text_outputs: list[str] = Field(default_factory=list)
    supported_trackers: list[str] = Field(default_factory=list)
    text_driven_trackers: list[str] = Field(default_factory=list)
    max_window_frames: int | None = None
    tracker_context_mode: Literal["none", "session"] = "none"
    max_context_frames: int | None = None
    supported_variants: list[dict[str, Any]] = Field(default_factory=list)
    variant_combinations: list[list[str]] = Field(default_factory=list)
    variants_shared_across_tasks: bool = False
    default_variants: dict[str, str] = Field(default_factory=dict)
    default_thresholds: dict[str, Any] = Field(default_factory=dict)
    resource_profile: dict[str, Any] = Field(default_factory=dict)
    composition: str = "atom"
    params: dict[str, Any] = Field(default_factory=dict)
    modality: str | None = None
    classes: list[dict[str, Any]] = Field(default_factory=list)


class ProjectMLBackendCapabilities(_AAPModel):
    name: str | None = None
    version: str | None = None
    protocol_version: str = "1"
    task_contract_revision: Literal[0, 1] = 0
    compat_protocol_versions: list[str] = Field(default_factory=list)
    model_version: str | None = None
    weights_version: str | None = None
    infra: str = "unknown"
    warmup_endpoint: bool = False
    managed_lifecycle: dict[str, Any] | None = None
    models: list[ProjectMLModelCapability] = Field(default_factory=list)
    is_interactive: bool = False
    supported_prompts: list[str] = Field(default_factory=list)
    supported_inputs: list[str] = Field(default_factory=list)
    supported_trackers: list[str] = Field(default_factory=list)
    text_driven_trackers: list[str] = Field(default_factory=list)
    supported_text_outputs: list[str] = Field(default_factory=list)
    supported_geometric_outputs: list[str] = Field(default_factory=list)
    modalities: list[str] = Field(default_factory=list)
    warnings: list[ProjectMLCapabilityWarning] = Field(default_factory=list)


class ProjectMLPredictionResult(_AAPModel):
    task_id: str
    result: Any = None
    score: float | None = None


class ProjectMLPredictTestResponse(_AAPModel):
    results: list[ProjectMLPredictionResult]


class ProjectMLPredictFrameResponse(_AAPModel):
    prediction_id: UUID
    candidate_count: int
    frame_index: int


class ProjectMLFramePredictRequest(_RequestModel):
    """Local frame path plus API form fields for synchronous video-frame inference."""

    frame_path: Path
    task_id: UUID
    frame_index: int
    config: dict[str, JsonValue] = Field(default_factory=dict)


class ProjectMLFrameInteractiveRequest(_RequestModel):
    """Local frame path plus the API's frame-indexed interactive context."""

    frame_path: Path
    task_id: UUID
    frame_index: int
    context: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("context")
    @classmethod
    def _context_body_bound(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > _MAX_INTERACTIVE_CONTEXT_BYTES:
            raise ValueError("context must be at most 1 MiB when serialized")
        return value


class ProjectMLWarmupRequest(RootModel[dict[str, JsonValue]]):
    """Backend-specific JSON forwarded unchanged to its warmup endpoint."""

    model_config = ConfigDict(allow_inf_nan=False)


class ProjectMLBackendOut(MLBackend):
    """Project-scoped registry result; auth tokens are never returned by the API."""

    is_interactive: bool = False
    auth_method: str = "none"
    extra_params: dict[str, Any] = Field(default_factory=dict)
    gpu_resource_id: str | None = None
    vram_budget_mb: int | None = None
    eviction_priority: int = 0
