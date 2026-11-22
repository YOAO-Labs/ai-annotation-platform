from __future__ import annotations

from typing import Literal

from yoao_task_contract import normalize_task
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelCapability(BaseModel):
    """能力声明协议 v2 — 单个 model 条目 (一个 backend 暴露 N 个).

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
    supported_inputs: list[str] = []
    default_input_type: str | None = None
    supported_geometric_outputs: list[str] = []
    output_attribute_types: list[str] = []
    output_attribute_schema: list[dict] = []
    supported_text_outputs: list[str] = []
    supported_trackers: list[str] = []
    text_driven_trackers: list[str] = []
    max_window_frames: int | None = Field(default=None, gt=0)
    tracker_context_mode: Literal["none", "session"] = "none"
    max_context_frames: int | None = Field(default=None, gt=0)
    supported_variants: list[dict] = []
    variant_combinations: list[list[str]] = []
    variants_shared_across_tasks: bool = False
    default_variants: dict[str, str] = {}
    default_thresholds: dict = {}
    resource_profile: dict = {}
    composition: str = "atom"
    params: dict = {}
    modality: str | None = None
    classes: list[dict] = []
