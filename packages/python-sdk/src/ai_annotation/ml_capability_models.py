"""Typed protocol vocabulary and public instance capability responses."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ai_annotation.models import _AAPModel


class SuggestedBackendItem(_AAPModel):
    name: str
    repo_url: str
    summary: str
    research_link: str | None = None
    infra: str | None = None
    builtin: bool = False


class ProtocolTaskItem(_AAPModel):
    id: str
    label: str
    summary: str
    default_geometry: list[str]
    default_modalities: list[str]
    typical_models: list[str]
    protocol_notes: str
    suggested_backends: list[SuggestedBackendItem] = []


class ProtocolInfraItem(_AAPModel):
    id: str
    label: str
    summary: str


class ProtocolModalityItem(_AAPModel):
    id: str
    label: str
    summary: str


class ProtocolGeometryItem(_AAPModel):
    id: str
    label: str
    summary: str


class ProtocolPromptItem(_AAPModel):
    id: str
    label: str
    summary: str
    requires_input: bool
    interactive_route: bool


class ProtocolInputItem(_AAPModel):
    id: str
    label: str
    summary: str


class InstanceVariantOption(_AAPModel):
    value: str
    label: str | None = None
    vram_gb: float | None = None
    tier: str | None = None
    recommended: bool = False
    note: str | None = None


class InstanceVariantGroup(_AAPModel):
    key: str
    title: str | None = None
    description: str | None = None
    variants: list[InstanceVariantOption] = []


class InstanceModelItem(_AAPModel):
    id: str
    display_name: str
    task: str = "unknown"
    model_family: str | None = None
    infra: str | None = None
    is_interactive: bool = False
    supported_prompts: list[str] = []
    supported_inputs: list[str] = []
    default_input_type: str | None = None
    resource_profile: dict[str, Any] = {}
    supported_geometric_outputs: list[str] = []
    supported_trackers: list[str] = []
    text_driven_trackers: list[str] = []
    max_window_frames: int | None = Field(default=None, gt=0)
    output_attribute_types: list[str] = []
    output_attribute_schema: list[dict[str, Any]] = []
    classes: list[dict[str, Any]] = []
    modality: str | None = None
    supported_variants: list[InstanceVariantGroup] = []
    variant_combinations: list[list[str]] = []
    variants_shared_across_tasks: bool = False
    default_variants: dict[str, str] = {}
    composition: str = "atom"


class CapabilityInstanceItem(_AAPModel):
    backend_id: str
    state: str
    source: str
    name: str
    infra: str = "unknown"
    warmup_endpoint: bool = False
    models: list[InstanceModelItem] = []


class CapabilityInstancesResponse(_AAPModel):
    instances: list[CapabilityInstanceItem]


class ProtocolCapabilitiesResponse(_AAPModel):
    version: str
    task_aliases: dict[str, str] = Field(default_factory=dict)
    tasks: list[ProtocolTaskItem]
    infras: list[ProtocolInfraItem]
    modalities: list[ProtocolModalityItem]
    geometries: list[ProtocolGeometryItem]
    prompts: list[ProtocolPromptItem] = []
    inputs: list[ProtocolInputItem] = []
