"""YOLO 预训练权重矩阵 + 文件名解析.

矩阵基于 ultralytics/assets release v8.3.0 + v8.4.0 (2026-06-08 核对). 与协议 v2
`/setup` 的 `models[].supported_variants` 严格对应:
- 每个 task (det/seg/pose/obb) 一份 series → sizes 映射
- /setup 生成 variants 时按此过滤, 前端只看到能加载的组合
- /predict 校验 (task, series, size) ∈ matrix, 否则 400

文件名解析约定:
- yolov8 系: yolov8{n,s,m,l,x}[-task].pt
- yolov9 系: yolov9{t,s,m,c,e}[-seg].pt (注意 t 仅 det, seg 仅 c/e)
- yolov10 系: yolov10{n,s,m,b,l,x}.pt (仅 det)
- yolo11 系: yolo11{n,s,m,l,x}[-task].pt
- yolo12 系: yolo12{n,s,m,l,x}.pt (仅 det)
- yolo26 系: yolo26{n,s,m,l,x}[-task].pt
- rtdetr 系: rtdetr-{l,x}.pt (仅 det, size 用 l/x 而非 nsmlx)
"""

from __future__ import annotations

from typing import Final

# 协议 v2 task id (与 capability_registry / aap_protocol_v2.vocab.TASK_VALUES 对齐).
TASK_DETECTION: Final[str] = "detection"
TASK_SEGMENTATION: Final[str] = "segmentation"
TASK_KEYPOINT: Final[str] = "keypoint"
TASK_OBB: Final[str] = "obb"
# 检测式视频追踪: 不是独立权重, 而是 detection 权重的 video 推理模式 (ultralytics
# 原生 ByteTrack/BoT-SORT)。故权重矩阵别名到 detection (见 MODEL_MATRIX 下方), 文件名后缀空。
TASK_TRACKER: Final[str] = "tracker"

# task → series → sizes (按 ultralytics/assets v8.3.0 + v8.4.0 实际有的预训练权重).
# 维护规则: ultralytics 发布新权重时, 改本表 + 协议 /setup 输出会自动跟随.
MODEL_MATRIX: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    TASK_DETECTION: {
        "yolov8": ("n", "s", "m", "l", "x"),
        "yolov9": ("t", "s", "m", "c", "e"),
        "yolov10": ("n", "s", "m", "b", "l", "x"),
        "yolo11": ("n", "s", "m", "l", "x"),
        "yolo12": ("n", "s", "m", "l", "x"),
        "yolo26": ("n", "s", "m", "l", "x"),
        "rtdetr": ("l", "x"),
    },
    TASK_SEGMENTATION: {
        "yolov8": ("n", "s", "m", "l", "x"),
        # yolov9-seg 官方仅放出 c/e 两档.
        "yolov9": ("c", "e"),
        "yolo11": ("n", "s", "m", "l", "x"),
        "yolo26": ("n", "s", "m", "l", "x"),
    },
    TASK_KEYPOINT: {
        "yolov8": ("n", "s", "m", "l", "x"),
        "yolo11": ("n", "s", "m", "l", "x"),
        "yolo26": ("n", "s", "m", "l", "x"),
    },
    TASK_OBB: {
        "yolov8": ("n", "s", "m", "l", "x"),
        "yolo11": ("n", "s", "m", "l", "x"),
        "yolo26": ("n", "s", "m", "l", "x"),
    },
}

# tracker 复用 detection 权重 (追踪不换权重, 只在推理时外挂关联算法)。别名共享同一
# series→sizes 对象, 保证与 det 矩阵零漂移; /setup 的 tracker 条目、is_supported、
# resolve_weight_filename 因此对 tracker 一致工作 (task=tracker → 解出 detection 权重文件名)。
MODEL_MATRIX[TASK_TRACKER] = MODEL_MATRIX[TASK_DETECTION]

# series 推荐选项 (UI 默认选中). 选 yolo11 因为兼顾精度 / 速度 / 任务覆盖.
RECOMMENDED_SERIES: Final[str] = "yolo11"
RECOMMENDED_SIZE: Final[str] = "s"

# size 元信息. tier 按 size 字母分档为「快速 / 均衡 / 精度」, 协议中性, 跨 series 通用。
# 移除 vram_gb: 之前是粗估占位 (n=2GB 等), 但 yolov8n .pt 实际加载 ~300MB,
# yolov8x ~500MB, 推理峰值还取决于 batch / 输入分辨率 / FP16/32, 暴露统一数字会误导。
# 等真有按 (series, size, task) 维度的官方 params/FLOPs 表再补。
SIZE_META: Final[dict[str, dict[str, object]]] = {
    "n": {"label": "nano", "tier": "fast"},
    "t": {"label": "tiny", "tier": "fast"},
    "s": {"label": "small", "tier": "balanced"},
    "m": {"label": "medium", "tier": "balanced"},
    "b": {"label": "balanced", "tier": "balanced"},
    "c": {"label": "compact", "tier": "balanced"},
    "l": {"label": "large", "tier": "accurate"},
    "e": {"label": "extreme", "tier": "accurate"},
    "x": {"label": "xlarge", "tier": "accurate"},
}

# series 元信息. label 是 UI 展示名.
SERIES_LABEL: Final[dict[str, str]] = {
    "yolov8": "YOLOv8",
    "yolov9": "YOLOv9",
    "yolov10": "YOLOv10",
    "yolo11": "YOLO11",
    "yolo12": "YOLO12",
    "yolo26": "YOLO26",
    "rtdetr": "RT-DETR",
}

# task → ultralytics 权重文件名后缀. detection 无后缀.
_TASK_SUFFIX: Final[dict[str, str]] = {
    TASK_DETECTION: "",
    TASK_SEGMENTATION: "-seg",
    TASK_KEYPOINT: "-pose",
    TASK_OBB: "-obb",
    # tracker 解出 detection 权重文件名 (无后缀), 与 MODEL_MATRIX 别名一致。
    TASK_TRACKER: "",
}


class UnsupportedVariantError(ValueError):
    """(task, series, size) 组合不在 MODEL_MATRIX 内."""


def is_supported(task: str, series: str, size: str) -> bool:
    return size in MODEL_MATRIX.get(task, {}).get(series, ())


def resolve_weight_filename(task: str, series: str, size: str) -> str:
    """返回 ultralytics 期望的权重文件名 (如 yolo11s-seg.pt / rtdetr-l.pt).

    缺失组合抛 UnsupportedVariantError. 文件名规则 (来自 ultralytics/assets release):
    - rtdetr 系列特殊: `rtdetr-{l,x}.pt`, 不带后缀 (RT-DETR 只有 detection)
    - 其它: `{series}{size}{task_suffix}.pt`, e.g. yolov8s.pt / yolo11l-pose.pt
    """
    if not is_supported(task, series, size):
        raise UnsupportedVariantError(
            f"task={task} series={series} size={size} not in MODEL_MATRIX"
        )
    if series == "rtdetr":
        return f"rtdetr-{size}.pt"
    suffix = _TASK_SUFFIX[task]
    return f"{series}{size}{suffix}.pt"


def iter_supported_combinations() -> list[tuple[str, str, str]]:
    """全部 (task, series, size) 组合扁平列表, 用于冒烟测试覆盖."""
    out: list[tuple[str, str, str]] = []
    for task, by_series in MODEL_MATRIX.items():
        for series, sizes in by_series.items():
            for size in sizes:
                out.append((task, series, size))
    return out


def series_options_for_task(task: str) -> list[str]:
    """该 task 下有预训练的全部 series, 顺序与 MODEL_MATRIX 一致 (= 协议 /setup 输出顺序)."""
    return list(MODEL_MATRIX.get(task, {}).keys())


def sizes_for(task: str, series: str) -> list[str]:
    """该 (task, series) 下有预训练的全部 size."""
    return list(MODEL_MATRIX.get(task, {}).get(series, ()))


# ── 开集 (open-vocabulary) 文本提示模型 ─────────────────────────────────────
# 与闭集 MODEL_MATRIX 并列, 独立 series 命名空间. 权重名按 ultralytics assets
# release v8.4.0 核对 (2026-06-25, 容器内 get_github_assets 实测可下载).
#   - YOLO-World (YOLOWorld 类): 仅检测, 文本 prompt 经 CLIP 文本编码.
#   - YOLOE (YOLOE 类): -seg 权重, 检测取 box / 分割取 mask,
#     文本 prompt 经 MobileCLIP. -seg-pf (prompt-free 内置词表) 非目标, 不纳入.
# pool key 的 task 分量统一用 POOL_TASK_OPENVOCAB: yoloe 的 det/seg 同权重共用一份.

POOL_TASK_OPENVOCAB: Final[str] = "openvocab"
# YOLOE visual prompt exemplar 用独立 pool key (与文本句柄隔离):
# VP 推理经 YOLOEVPSegPredictor 改写 model.names / 嵌入状态, 若与文本路径共用同一句柄会
# 污染 _aap_classes 文本 PE 缓存 (set_classes 被误判已设而跳过) → 单独一份 YOLOE 实例。
POOL_TASK_OPENVOCAB_VP: Final[str] = "openvocab_vp"

# /setup model 条目 → 该条目暴露的 series → sizes.
OPENVOCAB_WORLD_SERIES: Final[dict[str, tuple[str, ...]]] = {
    "yolo-worldv2": ("s", "m", "l", "x"),
    "yolo-world": ("s", "m", "l", "x"),
}
OPENVOCAB_YOLOE_SERIES: Final[dict[str, tuple[str, ...]]] = {
    "yoloe-v8": ("s", "m", "l"),
    "yoloe-11": ("s", "m", "l"),
    "yoloe-26": ("n", "s", "m", "l", "x"),
}

OPENVOCAB_FAMILY: Final[dict[str, str]] = {
    **{s: "world" for s in OPENVOCAB_WORLD_SERIES},
    **{s: "yoloe" for s in OPENVOCAB_YOLOE_SERIES},
}
OPENVOCAB_SERIES: Final[frozenset[str]] = frozenset(OPENVOCAB_FAMILY)

OPENVOCAB_SERIES_LABEL: Final[dict[str, str]] = {
    "yolo-worldv2": "YOLO-World v2",
    "yolo-world": "YOLO-World",
    "yoloe-v8": "YOLOE · v8",
    "yoloe-11": "YOLOE · 11",
    "yoloe-26": "YOLOE · 26",
}

# 各 model 条目的默认 (series, size). world 用 v2/s, yoloe 用 11/s (现代档 + 显存友好).
OPENVOCAB_DEFAULT_WORLD: Final[tuple[str, str]] = ("yolo-worldv2", "s")
OPENVOCAB_DEFAULT_YOLOE: Final[tuple[str, str]] = ("yoloe-11", "s")


def is_openvocab_series(series: str) -> bool:
    return series in OPENVOCAB_SERIES


def openvocab_family(series: str) -> str:
    """world | yoloe. 决定加载用 YOLOWorld 还是 YOLOE 类."""
    return OPENVOCAB_FAMILY[series]


def openvocab_sizes(series: str) -> tuple[str, ...]:
    return (
        OPENVOCAB_WORLD_SERIES.get(series) or OPENVOCAB_YOLOE_SERIES.get(series) or ()
    )


def is_openvocab_supported(series: str, size: str) -> bool:
    return size in openvocab_sizes(series)


def resolve_openvocab_weight_filename(series: str, size: str) -> str:
    """开集 series/size → ultralytics 权重文件名 (release v8.4.0 实有)."""
    if not is_openvocab_supported(series, size):
        raise UnsupportedVariantError(
            f"openvocab series={series} size={size} not available"
        )
    if series == "yolo-worldv2":
        return f"yolov8{size}-worldv2.pt"
    if series == "yolo-world":
        return f"yolov8{size}-world.pt"
    if series == "yoloe-v8":
        return f"yoloe-v8{size}-seg.pt"
    if series == "yoloe-11":
        return f"yoloe-11{size}-seg.pt"
    if series == "yoloe-26":
        return f"yoloe-26{size}-seg.pt"
    raise UnsupportedVariantError(f"unknown openvocab series {series}")
