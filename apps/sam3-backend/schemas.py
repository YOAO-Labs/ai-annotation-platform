"""Request / response Pydantic schemas, aligned with docs-site/dev/reference/ml-backend-protocol.md §2.

`Context.type` 在 grounded-sam2 的 point/bbox/polygon/text 基础上新增
`"exemplar"`: 取图中已有一个 bbox 作为视觉示例, 由 SAM 3 PCS 一步出全图相似实例的 masks.

`exemplar` 复用 `bbox` 字段承载 [x1, y1, x2, y2] (归一化 [0,1]), 语义靠 `type` 区分,
避免协议字段爆炸; apps/api 仅在项目挂了 sam3-backend 时才允许前端发起 exemplar 请求.

⚠️ sam3-backend 实际只实现 bbox/text/exemplar 三种 (放弃 point).
`Literal` 保留 "point" 是为了协议层一致性 (apps/api 同一 schema 跨多个 backend);
sam3-backend main.py 收到 type=point 时返回 400. /setup.supported_prompts 也明确
不含 "point", 前端按此动态启用工具.

通用部分 (TaskItem / PredictionResult / BatchPredictResponse) 来自
`apps/_shared/protocol_v2/` 共享包, 单一来源避免与 grounded-sam2-backend / yolo-backend
之间漂移. 本仓继续维护 sam3 特有的 Context + 请求壳.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from aap_protocol_v2 import (
    BatchPredictResponse,
    MAX_LOW_RES_MASK_INPUT_CHARS,
    MAX_SCRIBBLE_STROKES,
    MaskPromptPayload,
    PredictionResult,
    ScribblePrompt,
    ScribbleStroke,
    TaskItem,
    WarmupResponse,
)
from pydantic import BaseModel, Field, StrictInt, model_validator

MAX_SPARSE_PROMPT_POINTS = 512

__all__ = [
    "AnnotationResult",
    "BatchPredictRequest",
    "BatchPredictResponse",
    "Context",
    "Exemplar",
    "InteractiveRequest",
    "PredictionResult",
    "TaskItem",
    "WarmupResponse",
]


class Exemplar(BaseModel):
    """PCS 多正负框中的单个视觉示例。

    bbox: 归一化 xyxy [x1, y1, x2, y2]; label: True=正框(扩召回) / False=负框(排误检)。
    backend 顺序累加 (add_geometric_prompt), 每请求重发全量 (无状态)。
    """

    bbox: list[float]
    label: bool = True

    @model_validator(mode="after")
    def _validate_bbox(self) -> Exemplar:
        if len(self.bbox) != 4:
            raise ValueError("exemplar.bbox=[x1,y1,x2,y2] required (length 4)")
        # 与 yolo Exemplar 对齐: NaN/Inf 拒, [0,1] 越界 + 反向/退化框拒, 避免 backend 内部异常。
        if any(not math.isfinite(v) for v in self.bbox):
            raise ValueError("exemplar.bbox must be finite (no NaN/Inf)")
        x1, y1, x2, y2 = self.bbox
        if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
            raise ValueError(
                "exemplar.bbox must be normalized [x1,y1,x2,y2] with 0≤x1<x2≤1 and 0≤y1<y2≤1"
            )
        return self


class Context(BaseModel):
    # "interactive_box" = SAM-style 单框单 mask (开 inst 后); "bbox" 为纯几何形状,
    # 不再作交互 prompt (PCS 全图相似统一走 "exemplar"). "point" 升级为 inst 单实例点交互 (累加).
    type: Literal[
        "point",
        "interactive_box",
        "mask",
        "scribble",
        "polygon",
        "text",
        "exemplar",
    ]
    points: list[list[float]] | None = Field(
        default=None,
        max_length=MAX_SPARSE_PROMPT_POINTS,
    )
    labels: list[StrictInt] | None = Field(
        default=None,
        max_length=MAX_SPARSE_PROMPT_POINTS,
    )
    # bbox: type=interactive_box 时是单框 prompt; type=exemplar 时是单视觉示例框 (兼容旧单框路径)
    bbox: list[float] | None = None
    # type=exemplar 多正负框累加 (扩召回 / 去误检); 非空时优先于单 bbox.
    # 可与 text 同时传 (text 概念 + 几何示例组合)。
    exemplars: list[Exemplar] | None = None
    text: str | None = None
    mask_prompt: MaskPromptPayload | None = None
    scribbles: list[ScribbleStroke] | None = Field(
        default=None,
        max_length=MAX_SCRIBBLE_STROKES,
    )
    mask_input: str | None = Field(
        default=None,
        max_length=MAX_LOW_RES_MASK_INPUT_CHARS,
    )
    # (与 grounded-sam2 协议一致): text 路径输出形态
    output: Literal["box", "mask", "both"] = "mask"
    # shapely.simplify 像素级覆盖 (mask/both/exemplar 路径生效)
    simplify_tolerance: float | None = None
    # SAM 3 PCS exemplar / text 路径可选 score 阈值;
    # 缺省走 backend env SAM3_SCORE_THRESHOLD (默认 0.5). claude[bot] P2 · [0,1] 范围守卫。
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    # point / interactive_box 单点歧义时出 3 候选 (按 iou 降序); 缺省单 mask.
    multimask_output: bool = False
    output_geometry: Literal["polygon", "mask"] = "polygon"
    prompt_revision: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def _validate_required_fields(self) -> Context:
        if self.output_geometry == "mask" and not self.prompt_revision:
            raise ValueError(
                "context.prompt_revision required for output_geometry=mask"
            )
        if self.type == "exemplar":
            # 多框 exemplars 优先; 缺省退化单 bbox (旧路径兼容)。两者皆缺则报错。
            if not self.exemplars and (self.bbox is None or len(self.bbox) != 4):
                raise ValueError(
                    "type=exemplar requires non-empty context.exemplars[] "
                    "or context.bbox=[x1,y1,x2,y2]"
                )
        if self.type == "interactive_box":
            if self.bbox is None or len(self.bbox) != 4:
                raise ValueError(
                    "context.bbox=[x1,y1,x2,y2] required for type=interactive_box"
                )
            if any(not math.isfinite(value) for value in self.bbox):
                raise ValueError("context.bbox must be finite")
            x1, y1, x2, y2 = self.bbox
            if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
                raise ValueError("context.bbox must be normalized and non-degenerate")
        if self.type == "point":
            if not self.points:
                raise ValueError("context.points required for type=point")
            if self.labels is not None and len(self.labels) != len(self.points):
                raise ValueError("context.labels must match context.points length")
            for index, point in enumerate(self.points):
                if len(point) != 2 or any(not math.isfinite(value) for value in point):
                    raise ValueError(f"context.points[{index}] must be finite [x,y]")
                if not all(0.0 <= value <= 1.0 for value in point):
                    raise ValueError(f"context.points[{index}] must be normalized")
            if self.labels is not None and any(
                label not in (0, 1) for label in self.labels
            ):
                raise ValueError("context.labels values must be 0 or 1")
        if self.type == "mask" and self.mask_prompt is None:
            raise ValueError("context.mask_prompt required for type=mask")
        if self.type == "scribble":
            ScribblePrompt.model_validate(
                {
                    "type": "scribble",
                    "scribbles": self.scribbles,
                    "output_geometry": self.output_geometry,
                    "mask_prompt": self.mask_prompt,
                }
            )
            if (
                self.mask_prompt is None
                and self.mask_input is None
                and self.scribbles
                and all(stroke.polarity == 0 for stroke in self.scribbles)
            ):
                raise ValueError(
                    "negative-only scribble requires mask_prompt or mask_input"
                )
        if self.multimask_output and (
            self.mask_prompt is not None or self.mask_input is not None
        ):
            raise ValueError("multimask_output cannot be combined with a Mask seed")
        return self


class InteractiveRequest(BaseModel):
    task: TaskItem
    context: Context


class BatchPredictRequest(BaseModel):
    tasks: list[TaskItem]
    context: Context | None = None


class AnnotationResult(BaseModel):
    type: Literal["polygonlabels", "rectanglelabels"]
    value: dict[str, Any]
    score: float | None = None
