"""Mock 协议 v2.1 ML backend — 多模型目录 + variants + warmup + 错误形态演示 (无真实推理).

用途: 平台「能力声明协议 v2.1」端到端冒烟与接入参考。
- `/setup` 暴露 YOLO 风格多任务 models[] (detect / segment / pose / obb /
  classify) + PaddleOCR / DocLayout + 交互分割 / 视频追踪条目;
  yolo 条目演示 default_variants 与 variant_combinations (非全笛卡尔积)。
- `/predict` 按 `context.task` 与仅用于提示模式的 `context.type` 返回固定 demo 结果; OCR 条目在 result shape
  顶层带 `attributes.text`, 供平台 OCR adapter 提取 → annotation.attributes。
- `/warmup` 演示统一 WarmupResponse 形态 (ok / cache_hit / model_load_ms)。
- 非法 variant → 422 `variant_not_supported`; `size=x` 约定为「权重未下载」→
  503 `model_unavailable` + Retry-After (演示错误形态, 见 _resolve_variants)。

为保持示例零外部依赖 (仅 fastapi + pydantic), 错误体在本文件手写;
生产 backend 请直接用共享库 `apps/_shared/protocol_v2` 的
`VariantNotSupportedError` / `ModelUnavailableError` / `WarmupResponse`。

启动:
    pip install -r requirements.txt
    uvicorn main:app --host 0.0.0.0 --port 9100
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()


# series × size 两轴变体 (复用平台 supported_variants 多轴结构)。
_YOLO_VARIANTS = [
    {
        "key": "series",
        "title": "版本系列",
        "variants": [
            {"value": "yolov8", "label": "YOLOv8"},
            {"value": "yolo11", "label": "YOLO11", "recommended": True},
            {"value": "yolo12", "label": "YOLO12"},
        ],
    },
    {
        "key": "size",
        "title": "尺寸 / 精度档",
        "variants": [
            {"value": "n", "label": "nano", "vram_gb": 1, "tier": "fast"},
            {
                "value": "s",
                "label": "small",
                "vram_gb": 2,
                "tier": "balanced",
                "recommended": True,
            },
            {"value": "m", "label": "medium", "vram_gb": 4},
            {"value": "l", "label": "large", "vram_gb": 6},
            {"value": "x", "label": "xlarge", "vram_gb": 8, "tier": "accurate"},
        ],
    },
]

# 多轴非全笛卡尔积演示: mock 约定 yolo12 只有 n/s/m 三档,
# 其余 series 全 5 档。inner array 顺序与 supported_variants 轴顺序一致 [series, size]。
_YOLO_COMBINATIONS = [
    [series, size]
    for series in ("yolov8", "yolo11")
    for size in ("n", "s", "m", "l", "x")
] + [["yolo12", size] for size in ("n", "s", "m")]

# 各 model 自报的默认 variant 组合 (前端 VariantSelector 初值, 协议 §4.1.6)。
_YOLO_DEFAULT_VARIANTS = {"series": "yolo11", "size": "s"}


def _yolo_params() -> dict:
    return {
        "type": "object",
        "properties": {
            "conf": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "default": 0.25,
                "title": "置信度阈值",
                "x-platform-role": "confidence",
            },
            "iou": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "default": 0.7,
                "title": "NMS IoU",
                "x-platform-role": "iou",
            },
            "series": {
                "type": "string",
                "enum": ["yolov8", "yolo11", "yolo12"],
                "default": "yolo11",
                "title": "版本系列",
            },
            "size": {
                "type": "string",
                "enum": ["n", "s", "m", "l", "x"],
                "default": "s",
                "title": "尺寸",
            },
        },
    }


def _yolo_model(id_: str, display_name: str, task: str, geometry: str, **extra) -> dict:
    return {
        "id": id_,
        "display_name": display_name,
        "task": task,
        "model_family": "yolo",
        "infra": "pytorch",
        "supported_prompts": ["none"],
        "supported_geometric_outputs": [geometry],
        "supported_variants": _YOLO_VARIANTS,
        "variant_combinations": _YOLO_COMBINATIONS,
        "default_variants": _YOLO_DEFAULT_VARIANTS,
        "resource_profile": {"device": "gpu", "batchable": True},
        "params": _yolo_params(),
        **extra,
    }


MODELS = [
    _yolo_model(
        "yolo-detect",
        "YOLO 目标检测",
        "detect",
        "bbox",
        default_thresholds={"conf": 0.25, "iou": 0.7},
    ),
    _yolo_model("yolo-segment", "YOLO 实例分割", "segment", "polygon"),
    _yolo_model("yolo-pose", "YOLO 姿态 / 关键点", "pose", "keypoint"),
    _yolo_model("yolo-obb", "YOLO 旋转框", "obb", "rotated_bbox"),
    _yolo_model(
        "yolo-classify",
        "YOLO 图像分类",
        "classification",
        "none",
        output_attribute_types=["class"],
    ),
    {
        "id": "ppocr",
        "display_name": "PaddleOCR (mock)",
        "task": "ocr",
        "model_family": "paddleocr",
        "infra": "paddle",
        "supported_prompts": ["none"],
        "supported_geometric_outputs": ["polygon"],
        "output_attribute_types": ["text", "language"],
        "resource_profile": {"device": "cpu", "batchable": True},
        "params": {
            "type": "object",
            "properties": {
                "det_db_thresh": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "default": 0.3,
                    "title": "文本检测阈值",
                },
            },
        },
    },
    {
        "id": "doclayout",
        "display_name": "DocLayout-YOLO (mock)",
        "task": "doc_layout",
        "model_family": "doclayout-yolo",
        "infra": "onnx",
        "supported_prompts": ["none"],
        "supported_geometric_outputs": ["bbox"],
        "resource_profile": {"device": "cpu", "batchable": True},
        "params": {
            "type": "object",
            "properties": {
                "conf": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "default": 0.25,
                    "title": "置信度阈值",
                },
            },
        },
    },
    {
        "id": "screenshot-interactive",
        "display_name": "Screenshot interactive segmentation (mock)",
        "task": "interactive_seg",
        "model_family": "screenshot-stub",
        "infra": "onnx",
        "is_interactive": True,
        "supported_prompts": ["point", "interactive_box", "exemplar"],
        "supported_inputs": ["full_image", "point_prompt", "bbox_prompt"],
        "supported_geometric_outputs": ["polygon"],
        "exemplar_capabilities": {
            "multi_box": True,
            "negative_box": True,
            "text_combination": False,
            "threshold_refilter": True,
        },
        "resource_profile": {"device": "cpu", "batchable": False},
    },
    {
        "id": "screenshot-tracker",
        "display_name": "Screenshot video tracker (mock)",
        "task": "tracker",
        "model_family": "screenshot-stub",
        "infra": "onnx",
        "is_interactive": True,
        "supported_prompts": ["bbox"],
        "supported_inputs": ["video", "bbox_prompt"],
        "supported_geometric_outputs": ["bbox", "polygon"],
        "supported_trackers": ["sam3_video_interactive", "sam2_video"],
        "resource_profile": {"device": "cpu", "batchable": False},
    },
]


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/setup")
def setup() -> dict:
    # 协议 v2.1: backend 默认 infra=onnx, 各 model 条目可覆盖 (yolo→pytorch, ppocr→paddle)。
    return {
        "name": "mock-v2-backend",
        "version": "0.1.0",
        "protocol_version": "2.1",
        "task_contract_revision": 1,
        "compat_protocol_versions": ["2.0"],
        "model_version": "mock-v2",
        "is_interactive": True,
        "infra": "onnx",
        "warmup_endpoint": True,
        "models": MODELS,
    }


@app.get("/versions")
def versions() -> dict:
    return {"versions": ["mock-v2"]}


# ---------- variants 校验 (协议 §2.2 / §6 错误形态演示) ----------

_AXIS_ALLOWED = {
    axis["key"]: [v["value"] for v in axis["variants"]] for axis in _YOLO_VARIANTS
}


def _resolve_variants(variants: dict) -> dict:
    """校验 model_variants 并演示 422 / 503 两种标准错误形态。

    生产 backend 用 aap_protocol_v2.VariantNotSupportedError / ModelUnavailableError,
    HTTP 形态与这里手写的完全一致。
    """
    resolved = {**_YOLO_DEFAULT_VARIANTS, **variants}
    for axis, allowed in _AXIS_ALLOWED.items():
        value = resolved[axis]
        if value not in allowed:
            # 422: 值不在该轴枚举内。
            raise HTTPException(
                status_code=422,
                detail={
                    "error_code": "variant_not_supported",
                    "axis": axis,
                    "value": value,
                    "allowed": allowed,
                },
            )
    if [resolved["series"], resolved["size"]] not in _YOLO_COMBINATIONS:
        # 422: 两轴各自合法但组合不在 variant_combinations 内 (如 yolo12 + l)。
        allowed_sizes = [c[1] for c in _YOLO_COMBINATIONS if c[0] == resolved["series"]]
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "variant_not_supported",
                "axis": "size",
                "value": resolved["size"],
                "allowed": allowed_sizes,
            },
        )
    if resolved["size"] == "x":
        # 503 演示: mock 约定 size=x 视为「权重未下载」→ model_unavailable + Retry-After。
        key = f"{resolved['series']}/{resolved['size']}"
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "model_unavailable",
                "key": key,
                "reason": "checkpoint missing (mock: size=x 演示 503)",
            },
            headers={"Retry-After": "30"},
        )
    return resolved


class TaskItem(BaseModel):
    id: str
    file_path: str | None = None


class PredictRequest(BaseModel):
    tasks: list[TaskItem] = []
    task: TaskItem | None = None
    context: dict = {}


def _demo_shapes(context: dict) -> list[dict]:
    """按 task 返回固定 demo result (LabelStudio 风格 + OCR attributes)。"""
    task = context.get("task") or context.get("type")
    if task in {"tracker", "video_tracker"}:
        geometry = context.get("source_geometry") or {
            "type": "bbox",
            "x": 0.2,
            "y": 0.3,
            "w": 0.25,
            "h": 0.2,
        }
        return [
            {
                "frame_index": frame_index,
                "geometry": geometry,
                "confidence": 0.9,
                "outside": False,
            }
            for frame_index in range(
                int(context.get("from_frame", 0)),
                int(context.get("to_frame", 0)) + 1,
            )
        ]
    if task == "ocr":
        return [
            {
                "type": "rectanglelabels",
                "value": {
                    "x": 10,
                    "y": 8,
                    "width": 34,
                    "height": 6,
                    "rectanglelabels": ["text"],
                },
                "score": 0.96,
                "attributes": {"text": "发票号码 0012345", "language": "zh"},
            },
            {
                "type": "rectanglelabels",
                "value": {
                    "x": 10,
                    "y": 20,
                    "width": 42,
                    "height": 6,
                    "rectanglelabels": ["text"],
                },
                "score": 0.93,
                "attributes": {"text": "金额 ¥ 1,280.00", "language": "zh"},
            },
        ]
    if task == "doc_layout":
        return [
            {
                "type": "rectanglelabels",
                "value": {
                    "x": 8,
                    "y": 6,
                    "width": 60,
                    "height": 10,
                    "rectanglelabels": ["title"],
                },
                "score": 0.90,
            },
            {
                "type": "rectanglelabels",
                "value": {
                    "x": 8,
                    "y": 20,
                    "width": 80,
                    "height": 40,
                    "rectanglelabels": ["paragraph"],
                },
                "score": 0.88,
            },
        ]
    # detection 等几何任务默认: 单个 demo bbox。
    return [
        {
            "type": "rectanglelabels",
            "value": {
                "x": 12,
                "y": 15,
                "width": 25,
                "height": 30,
                "rectanglelabels": ["object"],
            },
            "score": 0.91,
        },
    ]


# /warmup 预热过的 variant key 集合 (mock 仅内存标记, 演示 cache_hit 翻转)。
_warmed: set[str] = set()


@app.post("/predict")
def predict(req: PredictRequest) -> dict:
    ctx = req.context or {}
    # v2.1 通用 axis dict; 兼容期继续接受旧字段 context.variants (yolo 风格) 并 normalize。
    variants = ctx.get("model_variants") or ctx.get("variants") or {}
    resolved = _resolve_variants(variants)
    shapes = _demo_shapes(ctx)
    key = f"{resolved['series']}/{resolved['size']}"
    cache_hit = key in _warmed
    _warmed.add(key)
    # 运行时观测字段 (协议 §4.2): cache_hit / model_load_ms / pool_state, 均为演示值。
    meta = {
        "score": 0.9,
        "model_version": f"mock-{key.replace('/', '-')}",
        "inference_time_ms": 5,
        "cache_hit": cache_hit,
        "model_load_ms": 0 if cache_hit else 120,
        "pool_state": {"current_size": min(len(_warmed), 4), "cap": 4},
    }
    # 交互式单条 (无外层 results 数组)。
    if req.task is not None and not req.tasks:
        return {"result": shapes, **meta}
    # 批量。
    results = [{"task": t.id, "result": shapes, **meta} for t in req.tasks]
    return {"results": results}


class WarmupRequest(BaseModel):
    task: str | None = None
    variants: dict = {}


@app.post("/warmup")
def warmup(req: WarmupRequest) -> dict:
    """显式预热 (协议 §4.4)。统一 WarmupResponse 形态: ok / cache_hit / model_load_ms。"""
    resolved = _resolve_variants(req.variants)
    key = f"{resolved['series']}/{resolved['size']}"
    if key in _warmed:
        return {"ok": True, "cache_hit": True, "model_load_ms": None}
    _warmed.add(key)
    return {"ok": True, "cache_hit": False, "model_load_ms": 120}
