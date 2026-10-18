"""v0.10.14 · E2 · ProjectTemplate 请求 / 响应 schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas._jsonb_types import (
    AttributeSchema,
    ClassConfigEntry,
    ClassesConfig,
    ProjectRenderingConfig,
    ToolBindings,
    validate_tool_bindings_keys,
)
from pydantic import field_validator


TemplateScope = Literal["private", "organization", "public"]


class ProjectTemplateBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    type_label: str = Field(min_length=1, max_length=50)
    type_key: str = Field(min_length=1, max_length=30)
    # v0.10.28 · 媒体维度数据类型 (image / video / lidar); 与 Project 对齐.
    data_type: str = "image"

    classes: list[str] = []
    classes_config: ClassesConfig | None = None
    attribute_schema: AttributeSchema | None = None
    # v0.10.17 · 工具维度类别 / 属性绑定; 优先于扁平字段, 兼容 Project schema.
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
    # E1 整合: 模板携带 markdown 文本; guide_assets 不入模板.
    annotation_guide: str | None = None

    scope: TemplateScope = "private"
    organization_id: UUID | None = None

    @field_validator("tool_bindings", mode="before")
    @classmethod
    def _check_tool_bindings_keys(cls, v):
        return validate_tool_bindings_keys(v)


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
    def _require_source_for_missing_fields(self) -> "ProjectTemplateCreate":
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
    # v0.10.17 · 工具维度类别 / 属性绑定.
    tool_bindings: ToolBindings | None = None
    label_config: dict | None = None
    ai_enabled: bool | None = None

    @field_validator("tool_bindings", mode="before")
    @classmethod
    def _check_tool_bindings_keys(cls, v):
        return validate_tool_bindings_keys(v)

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
    # v0.10.28 · 媒体维度数据类型 (image / video / lidar).
    data_type: str = "image"

    # v0.10.22 · 扁平视图字段不再有 DB 列, 由下方 validator 从 tool_bindings 派生.
    classes: list[str] = []
    classes_config: ClassesConfig = {}
    attribute_schema: AttributeSchema = AttributeSchema()
    # v0.10.17 · 工具维度类别 / 属性绑定 (唯一存储真值).
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
    def _derive_flat_class_views(self) -> "ProjectTemplateOut":
        # v0.10.22 · 从 tool_bindings 派生扁平投影 (无 DB 列).
        from aap_workspace_contract import (
            derive_attribute_schema,
            derive_classes_config,
            derive_classes_list,
        )

        tb = {
            k: (v.model_dump() if hasattr(v, "model_dump") else v)
            for k, v in (self.tool_bindings or {}).items()
        }
        self.classes = derive_classes_list(tb)
        self.classes_config = {
            name: ClassConfigEntry(**cfg)
            for name, cfg in derive_classes_config(tb).items()
        }
        self.attribute_schema = AttributeSchema(**derive_attribute_schema(tb))
        return self

    model_config = ConfigDict(from_attributes=True)
