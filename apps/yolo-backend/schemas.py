"""yolo-backend 特有的请求 Pydantic schema.

通用部分 (TaskItem / PredictionResult / BatchPredictResponse / WarmupResponse) 从
`aap_protocol_v2` 共享包引入 (apps/_shared/protocol_v2). 这里只放 yolo 的
Context: 与 sam3/gsam2 的 prompt 驱动不同, yolo 走纯批量 + variants 驱动.
"""

from __future__ import annotations

from aap_protocol_v2.model_metadata import BuiltinModelRef

import logging
import math
from typing import Literal

from aap_protocol_v2 import (
    BatchPredictResponse,
    PredictionResult,
    TaskItem,
    WarmupResponse,
    log_deprecated_model_variant_fields,
    normalize_context_model_variants,
)
from pydantic import BaseModel, Field, PrivateAttr, model_validator, field_validator
from aap_protocol_v2.predict import normalize_context_task
from aap_task_contract import normalize_task
from managed_models import ManagedModelRef
from sample_evaluations import EvaluationRequest

__all__ = [
    "BatchPredictRequest",
    "BatchPredictResponse",
    "Context",
    "Exemplar",
    "InteractiveRequest",
    "PredictParams",
    "PredictionResult",
    "TaskItem",
    "Variants",
    "WarmupRequest",
    "WarmupResponse",
]

logger = logging.getLogger("yolo-backend.schemas")


class Variants(BaseModel):
    """协议 v2 多轴 variants. yolo 用 series × size 两轴.

    series 含闭集 (yolov8…rtdetr) 与开集 (yolo-world(v2) / yoloe-*) 两个命名空间;
    开集 series 由 type=text 文本路径使用, 闭集由四 task 批量路径使用.
    """

    series: Literal[
        "yolov8",
        "yolov9",
        "yolov10",
        "yolo11",
        "yolo12",
        "yolo26",
        "rtdetr",
        # 开集文本检测 series.
        "yolo-worldv2",
        "yolo-world",
        "yoloe-v8",
        "yoloe-11",
        "yoloe-26",
    ]
    size: Literal["n", "t", "s", "m", "b", "c", "l", "e", "x"]


class PredictParams(BaseModel):
    """`/setup.params` 暴露的运行期可调超参. 与 ultralytics predict() 参数同义."""

    conf: float = Field(default=0.25, ge=0.0, le=1.0)
    iou: float = Field(default=0.70, ge=0.0, le=1.0)
    max_det: int = Field(default=300, ge=1, le=1000)
    # 检测式视频追踪的关联算法 (type=tracker 时生效; 其它 task 忽略)。平台 apply-time
    # 从 /setup.supported_trackers 选定并下发; enum 约束到 ultralytics 内建 tracker 配置文件名。
    tracker: Literal["bytetrack", "botsort"] = "bytetrack"


class Exemplar(BaseModel):
    """YOLOE visual prompt 单框样例 (字段名与 sam3 对齐, 平台 exemplar wire 直通).

    bbox: 归一化 xyxy [x1,y1,x2,y2] (∈[0,1]); label: True=正框 / False=负框。
    YOLOE 无负框语义 → predictor 仅取正框 (label=True), 负框静默忽略。
    """

    bbox: list[float]
    label: bool = True

    @model_validator(mode="after")
    def _validate_bbox(self) -> "Exemplar":
        if len(self.bbox) != 4:
            raise ValueError("exemplar.bbox=[x1,y1,x2,y2] required (length 4)")
        # 归一化坐标守卫: NaN/Inf 直接拒, [0,1] 越界 + 反向/退化框拒, 避免 ultralytics
        # 内部 500 或静默退化为空 tensor (issue claude[bot] P1)。
        if any(not math.isfinite(v) for v in self.bbox):
            raise ValueError("exemplar.bbox must be finite (no NaN/Inf)")
        x1, y1, x2, y2 = self.bbox
        if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
            raise ValueError(
                "exemplar.bbox must be normalized [x1,y1,x2,y2] with 0≤x1<x2≤1 and 0≤y1<y2≤1"
            )
        return self


class Context(BaseModel):
    """yolo context. `type` 决定走哪条分支:

    - 闭集批量: type ∈ {detection,segmentation,keypoint,obb}, 嵌套 params + 闭集 variants.
    - 开集文本: type="text", 顶层 `text` 开放词表 + 开集 variants(series=world/yoloe);
      平台文本路径把 conf/iou/max_det 扁平在顶层 (非嵌套 params), 由 before-validator 收拢.
    """

    task: Literal["detect", "segment", "pose", "obb", "interactive_seg", "tracker"] = (
        "detect"
    )
    type: Literal["text", "exemplar"] | None = None
    model_variants: dict[str, str] | None = None
    variants: Variants | None = None
    frozen_model_ref: ManagedModelRef | None = None
    builtin_model_ref: BuiltinModelRef | None = None
    sample_evaluation: EvaluationRequest | None = None
    _evaluation_profile: dict | None = PrivateAttr(default=None)
    _evaluation_meta: dict | None = PrivateAttr(default=None)
    _managed_receipt_digest: str | None = PrivateAttr(default=None)
    _managed_receipt: dict | None = PrivateAttr(default=None)
    _segmentation_meta: dict | None = PrivateAttr(default=None)
    params: PredictParams = Field(default_factory=PredictParams)
    # 类别白名单 (模型原生类别 index 子集). 非空时只检出这些类; 空/缺=全部类别.
    # 平台不做类→项目标签映射 (NG6), 仅在推理层用 ultralytics model.predict(classes=) 过滤.
    classes: list[int] | None = None
    # 开集文本路径字段 (type=text 时生效).
    text: str | None = None  # 开放词表, 逗号/换行分隔多类名.
    model_id: str | None = None  # 平台路由记录用 (后端按 series 派生 family, 不强依赖).
    output: Literal["box", "mask", "both"] = (
        "box"  # text 默认 box; exemplar 默认 mask 由平台下发.
    )
    # YOLOE visual prompt exemplar 路径 (type=exemplar 时生效).
    exemplars: list[Exemplar] | None = None  # 多框样例 (归一化 xyxy); 仅正框入 YOLOE。
    score_threshold: float | None = (
        None  # exemplar per-req 阈值 → 映射 yoloe conf; null=用 params.conf。
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_model_variants(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = normalize_context_task(data)
        model_tasks = {
            "detect": "detect",
            "segment": "segment",
            "pose": "pose",
            "obb": "obb",
            "detect-world": "detect",
            "detect-yoloe": "detect",
            "segment-yoloe": "segment",
            "exemplar-yoloe": "interactive_seg",
            "track": "tracker",
        }
        model_id = normalized.get("model_id")
        expected = (
            normalized.get("task") or "detect"
            if str(model_id or "").startswith("trained:")
            else model_tasks.get(model_id)
        )
        if expected is not None:
            normalized = normalize_context_task(normalized, model_task=expected)
        elif normalized.get("task") is None:
            normalized["task"] = (
                "interactive_seg"
                if normalized.get("type") == "exemplar"
                else (
                    "segment"
                    if normalized.get("type") == "text"
                    and normalized.get("output") in {"mask", "both"}
                    else "detect"
                )
            )
        normalized, deprecated = normalize_context_model_variants(normalized)
        log_deprecated_model_variant_fields(logger, deprecated)
        if normalized.get("frozen_model_ref"):
            normalized.pop("variants", None)
            return normalized
        if "variants" not in normalized and "model_variants" in normalized:
            normalized["variants"] = normalized["model_variants"]
        # exemplar 交互路径前端可能不带 model_variants (exemplar 工具暂无变体选择器);
        # exemplar 恒 yoloe, 缺省回落默认档 (与 gsam2 交互变体 env 兜底同理, 避免 422/502)。
        if "variants" not in normalized and normalized.get("type") == "exemplar":
            from model_registry import OPENVOCAB_DEFAULT_YOLOE  # noqa: PLC0415

            _series, _size = OPENVOCAB_DEFAULT_YOLOE
            normalized["variants"] = {"series": _series, "size": _size}
        # 文本路径 conf/iou/max_det 扁平在顶层 → 收拢成 params (闭集路径已嵌套, 跳过).
        if "params" not in normalized:
            flat = {
                k: normalized[k]
                for k in ("conf", "iou", "max_det")
                if k in normalized and normalized[k] is not None
            }
            if flat:
                normalized["params"] = flat
        return normalized

    @model_validator(mode="after")
    def _validate_combination(self) -> "Context":
        if self.sample_evaluation and (
            not self.frozen_model_ref
            or self.task not in {"detect", "segment"}
            or self.classes is not None
            or self.params.max_det != 100
        ):
            raise ValueError(
                "Sample evaluation requires controlled full-class detection with max_det=100"
            )
        # 真实组合是否在 MODEL_MATRIX 内, 留到 main /predict 处理;
        # schema 层只防住 enum 之外的 series/size 字符串拼写错误.
        if self.frozen_model_ref:
            expected_id = f"trained:{self.frozen_model_ref.model_version_id}"
            if (
                self.task not in {"detect", "segment"}
                or self.type is not None
                or self.model_id != expected_id
                or self.text
                or self.exemplars
            ):
                raise ValueError(
                    "Controlled weights support qualified image detection or segmentation only"
                )
            if self.task == "segment" and (
                self.classes is not None or self.params.max_det > 300
            ):
                raise ValueError(
                    "Controlled segmentation requires full classes and max_det <= 300"
                )
        elif (
            self.variants is None
            or (self.model_id or "").startswith("trained:")
            or self.model_id == "project-release"
        ):
            raise ValueError(
                "Builtin variants or a frozen controlled identity are required"
            )
        return self


def model_key_for_context(ctx: Context) -> tuple[str, str, str]:
    if ctx.frozen_model_ref:
        return (
            ctx.task,
            f"trained:{ctx.frozen_model_ref.model_version_id}",
            ctx.frozen_model_ref.sha256
            + (
                f":{ctx._managed_receipt_digest}" if ctx._managed_receipt_digest else ""
            ),
        )
    from model_registry import POOL_TASK_OPENVOCAB, POOL_TASK_OPENVOCAB_VP

    task = {"text": POOL_TASK_OPENVOCAB, "exemplar": POOL_TASK_OPENVOCAB_VP}.get(
        ctx.type, ctx.task
    )
    return task, ctx.variants.series, ctx.variants.size


class InteractiveRequest(BaseModel):
    """yolo 不做交互式, 保留 schema 名仅为与 sam3/gsam2 协议形态对齐 (单 task /predict 路径).

    实际 yolo 没有交互式分支, main 收到也走批量 predictor."""

    task: TaskItem
    context: Context


class BatchPredictRequest(BaseModel):
    tasks: list[TaskItem]
    context: Context

    @model_validator(mode="after")
    def _evaluation_has_one_sample(self):
        if self.context.sample_evaluation and len(self.tasks) != 1:
            raise ValueError("Sample evaluation requires exactly one task per request")
        return self

    @model_validator(mode="before")
    @classmethod
    def _accept_singular_task(cls, data):
        """平台交互调用 (predict_interactive) 向 /predict 发单数 `task` wire
        (`{task, context}`); 批量发 `tasks`。这里把单数归一成 `tasks=[task]`, 使 yolo 成为
        交互 backend (exemplar) 后, 同一 /predict 端点同时收批量与交互两种形态。"""
        if isinstance(data, dict) and "tasks" not in data and "task" in data:
            normalized = dict(data)
            normalized["tasks"] = [normalized.pop("task")]
            return normalized
        return data


class WarmupRequest(BaseModel):
    """协议 §4.4 `/warmup` 请求体. 与 predict context 结构相近, 但不带图像.

    task 取 /setup models[].task: 闭集四 task + 开集交互 exemplar 模型条目的 ``interactive_seg``
    (令 warmup 命中独立的 VP pool POOL_TASK_OPENVOCAB_VP, 与首次拖框交互同句柄; 见 issue 0003)。
    开集文本检测/分割条目的 task 仍是 detection/segmentation, 由 series 判定走文本 pool。"""

    task: Literal["detect", "segment", "pose", "obb", "interactive_seg"]
    variants: Variants

    @field_validator("task", mode="before")
    @classmethod
    def canonical_task(cls, value):
        return normalize_task(value)
