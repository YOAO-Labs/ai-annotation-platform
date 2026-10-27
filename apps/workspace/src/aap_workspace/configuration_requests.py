"""Project configuration and ProjectTemplate request/response models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .configuration_values import (
    AttributeSchema,
    ClassConfigEntry,
    ClassesConfig,
    ProjectRenderingConfig,
    ToolBindings,
    VideoCollaborationConfig,
    VideoSamplingConfig,
    validate_tool_bindings_keys,
)
from .mask_configuration_values import MaskQCConfig
from .point_quality_configuration_values import PointCloudQualityConfig


class ProjectCreate(BaseModel):
    name: str
    type_label: str | None = None
    type_key: str | None = None
    data_type: str | None = None
    classes: list[str] = []
    classes_config: ClassesConfig | None = None
    attribute_schema: AttributeSchema | None = None
    tool_bindings: ToolBindings | None = None

    @field_validator("tool_bindings", mode="before")
    @classmethod
    def _check_tool_bindings_keys(cls, value):
        return validate_tool_bindings_keys(value)

    ai_enabled: bool = False
    ml_backend_id: UUID | None = None
    ml_backend_source_id: UUID | None = None
    source_project_id: UUID | None = None
    copy_annotation_guide: bool = False
    template_id: UUID | None = None
    due_date: date | None = None
    box_threshold: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    text_threshold: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    ai_interactive_enabled: bool = True
    raster_mask_native_editing_enabled: bool = True
    video_sampling: VideoSamplingConfig | None = None
    video_collaboration: VideoCollaborationConfig | None = None

    @field_validator("video_collaboration")
    @classmethod
    def _video_collaboration_cannot_be_null(
        cls, value: VideoCollaborationConfig | None
    ) -> VideoCollaborationConfig:
        if value is None:
            raise ValueError("video_collaboration cannot be null")
        return value

    scene_mode: bool = False

    @model_validator(mode="after")
    def _validate_source_template_exclusive(self) -> ProjectCreate:
        if self.template_id is not None and self.source_project_id is not None:
            raise ValueError("template_id 与 source_project_id 互斥, 不能同时给")
        if self.type_label is None:
            if "type_label" in self.model_fields_set:
                raise ValueError("type_label 不能为 null")
            if self.template_id is None and self.source_project_id is None:
                raise ValueError(
                    "type_label 必填，除非提供 template_id 或 source_project_id"
                )
        if self.template_id is not None and self.copy_annotation_guide:
            raise ValueError("copy_annotation_guide 仅在 source_project_id 给定时有效")
        return self


class ProjectUpdate(BaseModel):
    name: str | None = None
    type_label: str | None = None
    type_key: str | None = None
    data_type: str | None = None
    status: str | None = None
    classes: list[str] | None = None
    classes_config: ClassesConfig | None = None
    attribute_schema: AttributeSchema | None = None
    tool_bindings: ToolBindings | None = None

    @field_validator("tool_bindings", mode="before")
    @classmethod
    def _check_tool_bindings_keys(cls, value):
        return validate_tool_bindings_keys(value)

    ai_enabled: bool | None = None
    ml_backend_id: UUID | None = None
    due_date: date | None = None
    sampling: str | None = None
    maximum_annotations: int | None = None
    show_overlap_first: bool | None = None
    iou_dedup_threshold: Annotated[float, Field(ge=0.3, le=0.95)] | None = None
    box_threshold: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    text_threshold: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    ai_interactive_enabled: bool | None = None
    raster_mask_native_editing_enabled: bool | None = None
    mask_qc_config: MaskQCConfig | None = None
    point_cloud_quality_config: PointCloudQualityConfig | None = None

    @field_validator("mask_qc_config")
    @classmethod
    def _mask_qc_config_cannot_be_null(cls, value: MaskQCConfig | None) -> MaskQCConfig:
        if value is None:
            raise ValueError("mask_qc_config cannot be null")
        return value

    @field_validator("point_cloud_quality_config")
    @classmethod
    def _point_cloud_quality_config_cannot_be_null(
        cls, value: PointCloudQualityConfig | None
    ) -> PointCloudQualityConfig:
        if value is None:
            raise ValueError("point_cloud_quality_config cannot be null")
        return value

    rendering_config: ProjectRenderingConfig | None = None
    annotation_guide: str | None = None
    guide_assets: list[dict] | None = None
    guide_assets_revision: Annotated[int, Field(strict=True, ge=1)] | None = None

    @field_validator("guide_assets", "guide_assets_revision")
    @classmethod
    def _guide_fields_cannot_be_null(cls, value):
        if value is None:
            raise ValueError("指南图片清单和修订号不可为 null")
        return value

    @model_validator(mode="after")
    def _guide_asset_revision_scope(self) -> ProjectUpdate:
        if (
            "guide_assets_revision" in self.model_fields_set
            and "guide_assets" not in self.model_fields_set
        ):
            raise ValueError("指南图片修订号须与清单一同提交")
        if self.guide_assets is not None:
            keys = [entry.get("key") for entry in self.guide_assets]
            if any(not isinstance(key, str) or not key for key in keys):
                raise ValueError("指南图片缺少资源标识")
            if len(set(keys)) != len(keys):
                raise ValueError("指南图片清单不可包含重复资源")
        return self

    video_sampling: VideoSamplingConfig | None = None
    video_collaboration: VideoCollaborationConfig | None = None

    @field_validator("video_collaboration")
    @classmethod
    def _video_collaboration_cannot_be_null(
        cls, value: VideoCollaborationConfig | None
    ) -> VideoCollaborationConfig:
        if value is None:
            raise ValueError("video_collaboration cannot be null")
        return value

    scene_mode: bool | None = None
    prefer_same_scene_continuation: bool | None = None
    scene_continuation_window_min: Annotated[int, Field(ge=1, le=480)] | None = None
    default_variants: dict[str, dict[str, str]] | None = None
    preannotate_pipeline: list[dict] | None = None


TemplateScope = Literal["private", "organization", "public"]


class ProjectTemplateBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    type_label: str = Field(min_length=1, max_length=50)
    type_key: str = Field(min_length=1, max_length=30)
    data_type: str = "image"

    classes: list[str] = []
    classes_config: ClassesConfig | None = None
    attribute_schema: AttributeSchema | None = None
    tool_bindings: ToolBindings | None = None
    label_config: dict | None = None
    ai_enabled: bool = False
    sampling: str = "sequence"
    maximum_annotations: int = 1
    show_overlap_first: bool = False
    iou_dedup_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.7
    box_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.35
    text_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.25
    rendering_config: ProjectRenderingConfig | None = None
    annotation_guide: str | None = None

    scope: TemplateScope = "private"
    organization_id: UUID | None = None

    @field_validator("tool_bindings", mode="before")
    @classmethod
    def _check_tool_bindings_keys(cls, value):
        return validate_tool_bindings_keys(value)


class ProjectTemplateCreate(ProjectTemplateBase):
    """新建模板.

    给定 ``source_project_id`` 时, service 层会自动从源项目 dump
    _CLONEABLE_PROJECT_FIELDS 进模板 (caller 传入的同字段值优先);
    ``annotation_guide`` 始终走 caller 传入或源项目兜底.
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    type_label: str | None = Field(default=None, min_length=1, max_length=50)
    type_key: str | None = Field(default=None, min_length=1, max_length=30)
    source_project_id: UUID | None = None

    @model_validator(mode="after")
    def _require_source_for_missing_fields(self) -> ProjectTemplateCreate:
        for name in ("name", "type_label", "type_key"):
            if getattr(self, name) is not None:
                continue
            if name in self.model_fields_set:
                raise ValueError(f"{name} 不能为 null")
            if self.source_project_id is None:
                raise ValueError(f"{name} 必填，除非提供 source_project_id")
        return self


class ProjectTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    type_label: str | None = None
    type_key: str | None = None
    data_type: str | None = None
    classes: list[str] | None = None
    classes_config: ClassesConfig | None = None
    attribute_schema: AttributeSchema | None = None
    tool_bindings: ToolBindings | None = None
    label_config: dict | None = None
    ai_enabled: bool | None = None

    @field_validator("tool_bindings", mode="before")
    @classmethod
    def _check_tool_bindings_keys(cls, value):
        return validate_tool_bindings_keys(value)

    sampling: str | None = None
    maximum_annotations: int | None = None
    show_overlap_first: bool | None = None
    iou_dedup_threshold: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    box_threshold: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    text_threshold: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    rendering_config: ProjectRenderingConfig | None = None
    annotation_guide: str | None = None
    scope: TemplateScope | None = None
    organization_id: UUID | None = None


class ProjectTemplateOut(BaseModel):
    id: UUID
    display_id: str
    name: str
    description: str | None = None
    type_label: str
    type_key: str
    data_type: str = "image"

    classes: list[str] = []
    classes_config: ClassesConfig = {}
    attribute_schema: AttributeSchema = AttributeSchema()
    tool_bindings: ToolBindings = {}
    label_config: dict = {}
    ai_enabled: bool = False
    sampling: str = "sequence"
    maximum_annotations: int = 1
    show_overlap_first: bool = False
    iou_dedup_threshold: float = 0.7
    box_threshold: float = 0.35
    text_threshold: float = 0.25
    rendering_config: ProjectRenderingConfig = ProjectRenderingConfig()
    annotation_guide: str | None = None

    scope: TemplateScope = "private"
    organization_id: UUID | None = None
    created_by: UUID
    created_by_name: str | None = None
    source_project_id: UUID | None = None
    usage_count: int = 0

    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _derive_flat_class_views(self) -> ProjectTemplateOut:
        from aap_workspace_contract import (
            derive_attribute_schema,
            derive_classes_config,
            derive_classes_list,
        )

        bindings = {
            key: (value.model_dump() if hasattr(value, "model_dump") else value)
            for key, value in (self.tool_bindings or {}).items()
        }
        self.classes = derive_classes_list(bindings)
        self.classes_config = {
            name: ClassConfigEntry(**config)
            for name, config in derive_classes_config(bindings).items()
        }
        self.attribute_schema = AttributeSchema(**derive_attribute_schema(bindings))
        return self

    model_config = ConfigDict(from_attributes=True)
