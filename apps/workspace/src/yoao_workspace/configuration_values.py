"""Portable value models used by Project and ProjectTemplate configuration."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

AttributeFieldType = Literal[
    "text", "number", "boolean", "select", "multiselect", "range"
]


class AttributeFieldOption(BaseModel):
    value: str
    label: str

    model_config = ConfigDict(extra="forbid")


class VisibleIfRule(BaseModel):
    """attribute field 的简单条件级联：当 other_key 等于 equals 时该字段才显示。"""

    key: str = Field(min_length=1)
    equals: Any | None = None

    model_config = ConfigDict(extra="forbid")


class AttributeField(BaseModel):
    key: str = Field(min_length=1)
    label: str
    type: AttributeFieldType
    required: bool | None = None
    default: Any | None = None
    options: list[AttributeFieldOption] | None = None
    min: float | None = None
    max: float | None = None
    regex: str | None = None
    applies_to: Literal["*"] | list[str] | None = None
    visible_if: VisibleIfRule | None = None
    hotkey: str | None = None
    description: str | None = None
    mutable: bool | None = None
    style_occluded: bool | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("hotkey")
    @classmethod
    def _check_hotkey(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if len(value) != 1 or not ("1" <= value <= "9"):
            raise ValueError("hotkey 必须是单个数字字符 1-9")
        return value


class AttributeSchema(BaseModel):
    fields: list[AttributeField] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check_unique(self) -> AttributeSchema:
        seen_keys: set[str] = set()
        seen_hotkeys: set[str] = set()
        for field in self.fields:
            if field.key in seen_keys:
                raise ValueError(f"attribute_schema.fields[].key 重复: {field.key!r}")
            seen_keys.add(field.key)
            if field.hotkey:
                if field.hotkey in seen_hotkeys:
                    raise ValueError(
                        f"attribute_schema.fields[].hotkey 重复: {field.hotkey!r}"
                    )
                seen_hotkeys.add(field.hotkey)
                if field.type not in {"boolean", "select"}:
                    raise ValueError(
                        f"hotkey 仅支持 boolean / select 字段：{field.key}"
                    )
            if field.type in {"select", "multiselect"} and not field.options:
                raise ValueError(
                    f"fields[{field.key!r}].options 必填且非空（{field.type} 类型）"
                )
            if field.style_occluded and field.type != "boolean":
                raise ValueError(
                    f"fields[{field.key!r}].style_occluded 仅支持 boolean 字段"
                )
        return self


class ClassConfigEntry(BaseModel):
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    order: int | None = Field(default=None, ge=0)
    alias: str | None = Field(
        default=None,
        max_length=50,
        pattern=r"^[a-zA-Z0-9 ,_\-]+$",
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("alias", mode="before")
    @classmethod
    def _normalize_alias(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        normalized = value.lower().strip()
        if not normalized:
            return None
        normalized = re.sub(r"\s*,[\s,]*", ",", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = normalized.strip(",").strip()
        return normalized or None


ClassesConfig = dict[str, ClassConfigEntry]

ToolUnitId = Literal[
    "bbox",
    "polyline",
    "region",
    "lidar_box_3d",
    "rotated_bbox",
    "keypoint",
    "point_mask_3d",
]
TOOL_UNIT_IDS: tuple[str, ...] = (
    "bbox",
    "polyline",
    "region",
    "lidar_box_3d",
    "rotated_bbox",
    "keypoint",
    "point_mask_3d",
)
RETIRED_AI_INTERACTIVE = "ai_interactive"
_REGION_GEOMETRY_TYPES = frozenset(
    {
        "polygon",
        "multi_polygon",
        "mask",
        "raster_mask",
        "video_mask",
        "video_polygon",
        "video_track_polygon",
        "video_track_mask",
    }
)


def map_retired_tool_unit(geometry_type: str | None) -> str:
    """把退役的 ai_interactive 按几何类型归位到 region / bbox。"""
    return "region" if geometry_type in _REGION_GEOMETRY_TYPES else "bbox"


class ClassRef(BaseModel):
    """v0.17.15 · 跨工具单位的类别引用 (alias_to 软关联链用).

    tool_unit_id 必须是合法 ToolUnitId; class_name 是目标 unit 内的类名.
    目标存在性不在此校验 (siblings 不可见), 由读时派生层解析时降级处理 (悬空 → 用自身值).
    """

    tool_unit_id: str = Field(min_length=1, max_length=30)
    class_name: str = Field(min_length=1, max_length=100)

    model_config = ConfigDict(extra="forbid")

    @field_validator("tool_unit_id")
    @classmethod
    def _check_unit(cls, value: str) -> str:
        if value not in TOOL_UNIT_IDS:
            raise ValueError(f"alias_to.tool_unit_id 非法: {value!r}")
        return value


class ToolClassEntry(BaseModel):
    """工具单位下的一条类别. name 必填且工具内唯一; color / alias 语义同 ClassConfigEntry."""

    name: str = Field(min_length=1, max_length=100)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    order: int | None = Field(default=None, ge=0)
    alias: str | None = Field(
        default=None,
        max_length=50,
        pattern=r"^[a-zA-Z0-9 ,_\-]+$",
    )
    alias_to: ClassRef | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("alias", mode="before")
    @classmethod
    def _normalize_alias(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        normalized = value.lower().strip()
        if not normalized:
            return None
        normalized = re.sub(r"\s*,[\s,]*", ",", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = normalized.strip(",").strip()
        return normalized or None


class KeypointNode(BaseModel):
    """关键点骨骼的一个节点（有序）。color 为可选 #RRGGBB。

    sublabel：可选第二层命名（label + sublabel 仅 2 层，禁止任意嵌套）。
    x / y：可选骨骼模板坐标（归一化 0–1），由 SVG 配置器拖点定义，仅作配置可视化
    与标注参考，不进实例几何。
    """

    name: str = Field(min_length=1)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    sublabel: str | None = Field(default=None)
    x: float | None = Field(default=None, ge=0, le=1)
    y: float | None = Field(default=None, ge=0, le=1)

    model_config = ConfigDict(extra="forbid")


class KeypointSchema(BaseModel):
    """关键点骨骼拓扑（COCO 范式，类别级元数据，不进实例几何）。

    nodes 有序，edges 每条是 [i, j] 两个节点索引的骨骼连线。
    """

    nodes: list[KeypointNode] = Field(default_factory=list)
    edges: list[list[int]] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @field_validator("edges")
    @classmethod
    def _check_edges(cls, value: list[list[int]]) -> list[list[int]]:
        for index, edge in enumerate(value):
            if len(edge) != 2:
                raise ValueError(f"edges[{index}] 必须是 [i, j] 两个节点索引")
            if edge[0] < 0 or edge[1] < 0:
                raise ValueError(f"edges[{index}] 节点索引必须 >= 0")
        return value


class VideoModesConfig(BaseModel):
    """视频几何工具单位的「单帧 / 轨迹」变体开关（每个视频单位各自携带一份）。

    box = 单帧几何变体, track = 轨迹几何变体。按单位解释：bbox 单位 → video_bbox /
    video_track_bbox；region 单位 → video_polygon / video_track_polygon；polyline 单位
    → video_polyline / video_track_polyline。多边形 / 折线是独立工具单位（各自类别 / 属性
    绑定），非 bbox 单位的子开关。仅用于工具栏可用性过滤; 不强制校验已存在的 annotation。
    None = 单帧 / 轨迹均可用（向后兼容老项目）。
    """

    box: bool = True
    track: bool = True

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _at_least_one_enabled(self) -> VideoModesConfig:
        if not (self.box or self.track):
            raise ValueError("video_modes 必须至少保留单帧 / 轨迹之一可用")
        return self


class ToolBinding(BaseModel):
    """单一工具单位下的 enable 状态 + 类别集合 + 属性 schema."""

    enabled: bool = False
    classes: list[ToolClassEntry] = Field(default_factory=list)
    attribute_schema: AttributeSchema = Field(default_factory=AttributeSchema)
    keypoint_schema: KeypointSchema | None = None
    video_modes: VideoModesConfig | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check_class_name_unique(self) -> ToolBinding:
        seen: set[str] = set()
        for item in self.classes:
            if item.name in seen:
                raise ValueError(
                    f"tool_binding.classes[].name 在工具单位内重复: {item.name!r}"
                )
            seen.add(item.name)
        return self


ToolBindings = dict[str, ToolBinding]


def validate_tool_bindings_keys(
    value: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """校验 ToolBindings 顶层 key 全在 TOOL_UNIT_IDS 内 (允许子集)."""
    if value is None:
        return value
    for key in value:
        if key not in TOOL_UNIT_IDS:
            raise ValueError(
                f"tool_bindings 顶层 key 必须是 {TOOL_UNIT_IDS} 之一, 收到: {key!r}"
            )
    return value


class ProjectRenderingConfig(BaseModel):
    smoothImage: bool | None = None
    cssImageFilter: str | None = Field(default=None, max_length=255)
    controlPointsSize: int | None = Field(default=None, ge=2, le=20)
    snapToGrid: bool | None = None
    box3dDefaultSize: tuple[float, float, float] | None = None
    propagateOverwrite: bool | None = None
    trackerDefaultModel: str | None = Field(default=None, max_length=128)

    model_config = ConfigDict(extra="forbid")

    @field_validator("box3dDefaultSize")
    @classmethod
    def _check_box3d_default_size(
        cls,
        value: tuple[float, float, float] | None,
    ) -> tuple[float, float, float] | None:
        if value is None:
            return value
        if any(size <= 0 for size in value):
            raise ValueError("box3dDefaultSize entries must be positive")
        return value


class VideoSamplingConfig(BaseModel):
    mode: Literal["none", "fps", "step"] = "none"
    target_fps: float | None = Field(default=None, gt=0)
    frame_step: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_mode_fields(self) -> VideoSamplingConfig:
        if self.mode == "fps":
            if self.target_fps is None:
                raise ValueError("mode=fps 必须提供 target_fps")
        elif self.mode == "step":
            if self.frame_step is None:
                raise ValueError("mode=step 必须提供 frame_step")
        elif self.target_fps is not None or self.frame_step is not None:
            raise ValueError("mode=none 时 target_fps / frame_step 应为 None")
        return self


class VideoCollaborationConfig(BaseModel):
    enabled: bool = False
    overlap_frames: int = Field(default=0, ge=0)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate_enabled_overlap(self) -> VideoCollaborationConfig:
        if self.enabled and self.overlap_frames < 1:
            raise ValueError("启用视频协同时 overlap_frames 必须大于 0")
        return self
