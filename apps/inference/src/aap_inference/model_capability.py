from __future__ import annotations

from typing import Literal

from aap_task_contract import normalize_task
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelCapability(BaseModel):
    """v0.14.9 · 能力声明协议 v2 — 单个 model 条目 (一个 backend 暴露 N 个).

    由 services/ml_capabilities._normalize_model 规范化后存入
    BackendCapabilities.models; `task` 是条目边界 (决定输出几何与项目兼容性),
    `infra` 缺省继承 backend 默认, `modality` 为派生视图。"""

    model_config = ConfigDict(extra="allow")

    id: str
    display_name: str | None = None
    task: str = "unknown"

    @field_validator("task", mode="before")
    @classmethod
    def canonical_task(cls, value):
        return normalize_task(value, allow_unknown=True)

    model_family: str | None = None
    infra: str = "unknown"
    is_interactive: bool = False
    supported_prompts: list[str] = []
    # 一等输入契约 (full_image/crop/bbox_prompt/point_prompt/video)。
    supported_inputs: list[str] = []
    default_input_type: str | None = None
    supported_geometric_outputs: list[str] = []
    output_attribute_types: list[str] = []
    # 协议③ · 属性 schema 自描述 ([{key,label,type,options}]), 供平台导入项目 attribute_schema.
    output_attribute_schema: list[dict] = []
    supported_text_outputs: list[str] = []
    supported_trackers: list[str] = []
    # text-driven tracker (sam3_video) 子集; propagate 需 text/exemplars。
    text_driven_trackers: list[str] = []
    max_window_frames: int | None = Field(default=None, gt=0)
    tracker_context_mode: Literal["none", "session"] = "none"
    max_context_frames: int | None = Field(default=None, gt=0)
    supported_variants: list[dict] = []
    # 多轴 variants 非笛卡尔积时显式列举合法组合 (前端目录展开用).
    variant_combinations: list[list[str]] = []
    # 同 backend 多 task 是否共享物理权重 (SAM 类共享, yolo 分 task).
    variants_shared_across_tasks: bool = False
    # backend 自报的默认 variant 组合 (dict[axis_key, value]).
    default_variants: dict[str, str] = {}
    default_thresholds: dict = {}
    resource_profile: dict = {}
    # 协议 v2.2 · 原子 vs 内部编排: 缺省 atom（老 backend 无字段即按原子处理）。
    composition: str = "atom"
    params: dict = {}
    modality: str | None = None
    # 闭集检测器原生类别表 ([{index,name}], 读自权重 model.names).
    classes: list[dict] = []
