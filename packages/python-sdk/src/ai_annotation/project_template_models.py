"""Typed request and response models for project templates."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_annotation.models import _AAPModel

TemplateScope = Literal["private", "organization", "public"]


class _RequestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        protected_namespaces=("model_validate", "model_dump"),
    )


class ProjectTemplateCreate(_RequestModel):
    """Create a template directly, or derive its fields from a visible project."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    type_label: str | None = Field(default=None, min_length=1, max_length=50)
    type_key: str | None = Field(default=None, min_length=1, max_length=30)
    data_type: str = "image"
    classes: list[str] = Field(default_factory=list)
    classes_config: dict[str, Any] | None = None
    attribute_schema: dict[str, Any] | None = None
    tool_bindings: dict[str, Any] | None = None
    label_config: dict[str, Any] | None = None
    ai_enabled: bool = False
    sampling: str = "sequence"
    maximum_annotations: int = 1
    show_overlap_first: bool = False
    iou_dedup_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    box_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    text_threshold: float = Field(default=0.25, ge=0.0, le=1.0)
    rendering_config: dict[str, Any] | None = None
    annotation_guide: str | None = None
    scope: TemplateScope = "private"
    organization_id: UUID | None = None
    source_project_id: UUID | None = None

    @model_validator(mode="after")
    def source_or_identity_fields(self) -> ProjectTemplateCreate:
        identity_fields = ("name", "type_label", "type_key")
        explicit_nulls = [
            name
            for name in identity_fields
            if name in self.model_fields_set and getattr(self, name) is None
        ]
        if explicit_nulls:
            raise ValueError(
                f"{', '.join(explicit_nulls)} cannot be null when creating a template"
            )
        if self.source_project_id is None and any(
            getattr(self, name) is None for name in identity_fields
        ):
            raise ValueError(
                "name, type_label, and type_key are required without source_project_id"
            )
        if self.scope == "organization" and self.organization_id is None:
            raise ValueError("organization scope requires organization_id")
        return self


class ProjectTemplateUpdate(_RequestModel):
    """Partial update; only fields set by the caller are sent as JSON."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    type_label: str | None = Field(default=None, min_length=1, max_length=50)
    type_key: str | None = Field(default=None, min_length=1, max_length=30)
    data_type: str | None = None
    classes: list[str] | None = None
    classes_config: dict[str, Any] | None = None
    attribute_schema: dict[str, Any] | None = None
    tool_bindings: dict[str, Any] | None = None
    label_config: dict[str, Any] | None = None
    ai_enabled: bool | None = None
    sampling: str | None = None
    maximum_annotations: int | None = None
    show_overlap_first: bool | None = None
    iou_dedup_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    box_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    text_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    rendering_config: dict[str, Any] | None = None
    annotation_guide: str | None = None
    scope: TemplateScope | None = None
    organization_id: UUID | None = None


class ProjectTemplate(_AAPModel):
    """Project template returned by the API; unknown response fields are retained."""

    id: UUID
    display_id: str
    name: str
    description: str | None = None
    type_label: str
    type_key: str
    data_type: str = "image"
    classes: list[str] = Field(default_factory=list)
    classes_config: dict[str, Any] = Field(default_factory=dict)
    attribute_schema: dict[str, Any] = Field(default_factory=dict)
    tool_bindings: dict[str, Any] = Field(default_factory=dict)
    label_config: dict[str, Any] = Field(default_factory=dict)
    ai_enabled: bool = False
    sampling: str = "sequence"
    maximum_annotations: int = 1
    show_overlap_first: bool = False
    iou_dedup_threshold: float = 0.7
    box_threshold: float = 0.35
    text_threshold: float = 0.25
    rendering_config: dict[str, Any] = Field(default_factory=dict)
    annotation_guide: str | None = None
    scope: TemplateScope = "private"
    organization_id: UUID | None = None
    created_by: UUID
    created_by_name: str | None = None
    source_project_id: UUID | None = None
    usage_count: int = 0
    created_at: datetime
    updated_at: datetime
