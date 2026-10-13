"""Request / response Pydantic schemas, aligned with docs-site/dev/ml-backend-protocol.md §2.

通用部分 (TaskItem / PredictionResult / BatchPredictResponse) 来自
`apps/_shared/protocol_v2/` 共享包, 单一来源避免与 sam3-backend / yolo-backend 之间
漂移. 本仓继续维护 grounded-sam2 特有的 Context + AnnotationValue + 请求壳.
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
    "AnnotationValue",
    "BatchPredictRequest",
    "BatchPredictResponse",
    "Context",
    "InteractiveRequest",
    "PredictionResult",
    "TaskItem",
    "WarmupRequest",
    "WarmupResponse",
]


class Context(BaseModel):
    # "interactive_box" = 单框单 mask, 双 backend 同名; "bbox" 已退出交互 prompt
    # 命名空间 (仅作纯几何形状).
    type: Literal[
        "point",
        "interactive_box",
        "mask",
        "scribble",
        "polygon",
        "text",
    ]
    points: list[list[float]] | None = Field(
        default=None,
        max_length=MAX_SPARSE_PROMPT_POINTS,
    )
    labels: list[StrictInt] | None = Field(
        default=None,
        max_length=MAX_SPARSE_PROMPT_POINTS,
    )
    bbox: list[float] | None = None
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
    # point / interactive_box 单点歧义时出 3 候选 (按 SAM iou 降序); 缺省单 mask.
    multimask_output: bool = False
    output_geometry: Literal["polygon", "mask"] = "polygon"
    prompt_revision: str | None = Field(default=None, max_length=256)
    # text 模式输出形态选择 (老前端不传时仍走 mask 兼容旧行为)
    # box: 仅 DINO 出框, 跳过 SAM image embedding + mask 推理 + cv2/shapely 简化, 速度最快
    # mask: DINO → SAM mask → polygon (当前默认行为)
    # both: 同 instance 配对返回 rectanglelabels + polygonlabels
    # point/interactive_box 类型下此字段无意义 (始终走 SAM mask → polygon 路径)
    output: Literal["box", "mask", "both"] = "mask"
    # 项目级 DINO 阈值注入 (text 路径生效). [0,1] 范围守卫。
    box_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    text_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    # shapely.simplify tolerance 像素级覆盖 (None 走 predictor.DEFAULT_SIMPLIFY_TOLERANCE).
    # 仅 mask/both 路径有意义 (box 路径不简化); 大物体可调高 (2-3) 减顶点, 精细物体调低 (0.3-0.5).
    simplify_tolerance: float | None = None

    @model_validator(mode="after")
    def _validate_native_mask_revision(self) -> Context:
        if self.output_geometry == "mask" and not self.prompt_revision:
            raise ValueError(
                "context.prompt_revision required for output_geometry=mask"
            )
        if self.type == "mask" and self.mask_prompt is None:
            raise ValueError("context.mask_prompt required for type=mask")
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
    context: Context | None = None  # 批量带文本 prompt 时附带


class WarmupRequest(BaseModel):
    """协议 §4.4 · /warmup 请求体.

    gsam2 同时预热 SAM + DINO 两个权重; task 字段 (detection/segmentation/
    interactive_seg/tracker) 决定 variants 必填项: detection 只需 dino_variant,
    interactive_seg/tracker 只需 sam_variant, segmentation 两个都需要.
    实际加载逻辑用 ModelPool, 缺失字段回退 backend env 默认.
    """

    task: Literal["detection", "segmentation", "interactive_seg", "tracker"] | None = (
        None
    )
    variants: dict[str, str] = {}


class AnnotationValue(BaseModel):
    points: list[list[float]] | None = None
    polygonlabels: list[str] | None = None
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None
    rectanglelabels: list[str] | None = None


class AnnotationResult(BaseModel):
    type: Literal["polygonlabels", "rectanglelabels"]
    value: dict[str, Any]
    score: float | None = None
