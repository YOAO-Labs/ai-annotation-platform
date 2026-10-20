"""Capability metadata; canonical visual task names are shared across services."""

from __future__ import annotations

from dataclasses import dataclass, field
from aap_task_contract import TASK_VALUES as CANONICAL_TASK_VALUES


@dataclass(frozen=True)
class SuggestedBackend:
    """协议卡的接入引导. 既可以是「外部推荐」也可以是「平台自带参考实现」(后者用
    `builtin=True` 标识, 前端会用不同视觉权重展示)。"""

    name: str
    repo_url: str
    summary: str
    research_link: str | None = None  # 指向 docs/research/ 内的本平台调研结论
    infra: str | None = None  # 推荐 backend 的默认 infra (与受控 INFRAS 对齐)
    builtin: bool = False  # True = 平台自带, 已在 docker-compose 提供; False = 外部推荐


@dataclass(frozen=True)
class TaskSpec:
    id: str
    label: str  # 中文短标签 (UI 主标题)
    summary: str  # 一句话简介 (协议层语义, 不写「平台支持 N 个模型」)
    default_geometry: tuple[str, ...]  # task 未声明几何时按此补全
    default_modalities: tuple[str, ...]  # UI 徽标用
    typical_models: tuple[str, ...]  # 协议示例, 仅文档用途
    protocol_notes: str  # 在协议 v2 下的输出约束 (引用 ml-backend-protocol §4.1)
    suggested_backends: tuple[SuggestedBackend, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class InfraSpec:
    id: str
    label: str
    summary: str


@dataclass(frozen=True)
class ModalitySpec:
    id: str
    label: str
    summary: str


@dataclass(frozen=True)
class GeometrySpec:
    id: str
    label: str
    summary: str


@dataclass(frozen=True)
class PromptSpec:
    """supported_prompts 的一条合法值。两维度元数据消解前后端「interactive」语义漂移:

    - requires_input: 需用户/上游显式给输入 (点/框/文本/示例), 含这类 prompt 的模型不默认走
      crop 投递。等价旧 `ml_capabilities._INTERACTIVE_PROMPTS` 语义 (含 text)。
    - interactive_route: 进画布交互工具线 (逐 prompt 路由到交互后端)。等价旧前端
      `useBackendRouting.INTERACTIVE_PROMPTS` 语义 (不含 text — text 归批量线)。
    """

    id: str
    label: str
    summary: str
    requires_input: bool
    interactive_route: bool


@dataclass(frozen=True)
class InputSpec:
    id: str
    label: str
    summary: str


# ── 9 个 task (协议 v2 受控词表, 顺序即 UI 展示顺序) ───────────────────
TASKS: tuple[TaskSpec, ...] = (
    TaskSpec(
        id="detect",
        label="目标检测",
        summary="在图像或视频帧中输出 bbox + 类别标签。",
        default_geometry=("bbox",),
        default_modalities=("image", "video"),
        typical_models=("YOLO 系", "DETR 系", "Grounding DINO", "SAM3"),
        protocol_notes=(
            "/predict 响应 result.type=rectanglelabels, "
            "value 含 x/y/width/height + rectanglelabels[]; 协议 v2 §4.1 范例 A。"
        ),
        suggested_backends=(
            SuggestedBackend(
                name="Grounded-SAM-2 (gsam2)",
                repo_url="https://github.com/IDEA-Research/Grounded-SAM-2",
                summary="平台自带参考实现, docker-compose 内置. 文本/框 prompt 驱动开放词汇检测。",
                infra="pytorch",
                builtin=True,
            ),
            SuggestedBackend(
                name="SAM3 (sam3)",
                repo_url="https://github.com/facebookresearch/segment-anything",
                summary="平台自带参考实现, docker-compose 内置. SAM 系列检测/分割/交互一体。",
                infra="pytorch",
                builtin=True,
            ),
            SuggestedBackend(
                name="Label Studio ML Backend (YOLO)",
                repo_url="https://github.com/HumanSignal/label-studio-ml-backend",
                summary="官方示例覆盖 YOLOv8 / DETR, 适合上手。",
                research_link="docs/research/01-label-studio.md",
                infra="pytorch",
            ),
            SuggestedBackend(
                name="X-AnyLabeling",
                repo_url="https://github.com/CVHub520/X-AnyLabeling",
                summary="本平台 v0.14.x 重点参考的开源端, 自带多种检测模型。",
                research_link="docs/research/04-x-anylabeling.md",
                infra="pytorch",
            ),
        ),
    ),
    TaskSpec(
        id="obb",
        label="旋转框",
        summary="在图像中输出带旋转角度的矩形 (Oriented Bounding Box)。",
        default_geometry=("rotated_bbox",),
        default_modalities=("image",),
        typical_models=("YOLOv8-OBB", "Rotated DETR", "Oriented R-CNN"),
        protocol_notes=(
            "/predict 响应 value 含 x/y/width/height/rotation; "
            "rotation 单位为度 (0-360)。"
        ),
        suggested_backends=(
            SuggestedBackend(
                name="X-AnyLabeling (YOLOv8-OBB)",
                repo_url="https://github.com/CVHub520/X-AnyLabeling",
                summary="自带 OBB 模型, 协议直连本平台。",
                research_link="docs/research/04-x-anylabeling.md",
            ),
        ),
    ),
    TaskSpec(
        id="segment",
        label="实例分割",
        summary="为每个对象输出 polygon / multi_polygon 几何 + 类别。",
        default_geometry=("polygon",),
        default_modalities=("image",),
        typical_models=("Mask R-CNN", "YOLO-Seg", "SegFormer", "SAM3"),
        protocol_notes=(
            "/predict 响应 result.type=polygonlabels, value.points 为闭合多边形; "
            "可由平台 mask→polygon 转换器从 mask 像素输出派生。"
        ),
        suggested_backends=(
            SuggestedBackend(
                name="Grounded-SAM-2 (gsam2)",
                repo_url="https://github.com/IDEA-Research/Grounded-SAM-2",
                summary="平台自带参考实现, docker-compose 内置. mask 输出经平台转 polygon 落库。",
                infra="pytorch",
                builtin=True,
            ),
            SuggestedBackend(
                name="SAM3 (sam3)",
                repo_url="https://github.com/facebookresearch/segment-anything",
                summary="平台自带参考实现, docker-compose 内置. 高质量 SAM 系分割。",
                infra="pytorch",
                builtin=True,
            ),
            SuggestedBackend(
                name="Label Studio ML Backend (Mask R-CNN)",
                repo_url="https://github.com/HumanSignal/label-studio-ml-backend",
                summary="官方示例含 Mask R-CNN。",
                research_link="docs/research/01-label-studio.md",
                infra="pytorch",
            ),
        ),
    ),
    TaskSpec(
        id="pose",
        label="关键点",
        summary="输出 2D 关键点集合 (含可见性), 如人体姿态、面部关键点。",
        default_geometry=("keypoint",),
        default_modalities=("image",),
        typical_models=("YOLOv8-Pose", "HRNet", "OpenPose"),
        protocol_notes=(
            "/predict 响应 result.type=keypointlabels; 每点含 x/y/visibility, "
            "建议同时声明 skeleton 边以便平台渲染连线。"
        ),
        suggested_backends=(
            SuggestedBackend(
                name="X-AnyLabeling (YOLOv8-Pose)",
                repo_url="https://github.com/CVHub520/X-AnyLabeling",
                summary="提供 YOLOv8-Pose 关键点模型。",
                research_link="docs/research/04-x-anylabeling.md",
            ),
        ),
    ),
    TaskSpec(
        id="classify",
        label="图像分类",
        summary="对整张图或裁片输出类别标签, 无几何输出。",
        default_geometry=("none",),
        default_modalities=("image",),
        typical_models=("ResNet", "ViT", "EfficientNet"),
        protocol_notes=(
            "/predict 响应 result.type=choices, value.choices 为类别名数组; "
            "supported_geometric_outputs=['none']。"
        ),
        suggested_backends=(
            SuggestedBackend(
                name="Label Studio ML Backend (HuggingFace classifier)",
                repo_url="https://github.com/HumanSignal/label-studio-ml-backend",
                summary="HuggingFace transformer 分类示例。",
                research_link="docs/research/01-label-studio.md",
            ),
        ),
    ),
    TaskSpec(
        id="ocr",
        label="OCR",
        summary="从图像中提取文本框与文本内容 (v0.14.9 首发模型族)。",
        default_geometry=("bbox",),
        default_modalities=("image",),
        typical_models=("PaddleOCR", "EasyOCR", "RapidOCR"),
        protocol_notes=(
            "/predict 响应 result.value 含 bbox + attributes.text (必填), "
            "可选 attributes.language / attributes.orientation; 协议 v2 §4.1 范例 C。"
        ),
        suggested_backends=(
            SuggestedBackend(
                name="PaddleOCR (via Label Studio ML)",
                repo_url="https://github.com/PaddlePaddle/PaddleOCR",
                summary="工业级 OCR, Paddle infra; 可包装为 ML backend。",
                research_link=None,
            ),
            SuggestedBackend(
                name="RapidOCR (ONNX)",
                repo_url="https://github.com/RapidAI/RapidOCR",
                summary="ONNX runtime, CPU 友好, 部署简单。",
                research_link=None,
            ),
        ),
    ),
    TaskSpec(
        id="doc_layout",
        label="版面分析",
        summary="对文档页面输出版面元素区块 (标题/段落/表格/插图等)。",
        default_geometry=("bbox",),
        default_modalities=("image",),
        typical_models=("LayoutLMv3", "PP-StructureV2", "DiT"),
        protocol_notes=(
            "/predict 响应 result.value 含 bbox + class_name "
            "(受控: title/paragraph/table/figure/formula/list/header/footer); "
            "协议 v2 §4.1 范例 C。"
        ),
        suggested_backends=(
            SuggestedBackend(
                name="PP-StructureV2",
                repo_url="https://github.com/PaddlePaddle/PaddleOCR/tree/main/ppstructure",
                summary="Paddle 版面分析, 含 table / formula 识别。",
                research_link=None,
            ),
        ),
    ),
    TaskSpec(
        id="tracker",
        label="视频追踪",
        summary="跨帧追踪同一对象, 输出对象 id + 每帧 bbox。",
        default_geometry=(),
        default_modalities=("video",),
        typical_models=("ByteTrack", "OC-SORT", "SAM2 Video"),
        protocol_notes=(
            "/setup.supported_trackers 声明可用 tracker; /predict 在视频帧上"
            "返回带 instance_id 的 bbox 序列。"
            "text-driven tracker (如 sam3_video) 额外在 /setup.text_driven_trackers "
            "声明, propagate 请求带 text/exemplars (而非仅 seed bbox), 每窗按 text 重检测。"
        ),
        suggested_backends=(
            SuggestedBackend(
                name="Grounded-SAM-2 (gsam2)",
                repo_url="https://github.com/IDEA-Research/Grounded-SAM-2",
                summary="平台自带参考实现, docker-compose 内置. 含 SAM2 Video 视频追踪能力。",
                infra="pytorch",
                builtin=True,
            ),
        ),
    ),
    TaskSpec(
        id="interactive_seg",
        label="交互分割",
        summary="基于用户 prompt (点/框/文本) 对图像做交互式实例分割。",
        default_geometry=("polygon",),
        default_modalities=("image",),
        typical_models=("SAM", "SAM2", "SAM3", "Grounded-SAM"),
        protocol_notes=(
            "/setup.supported_prompts 含 point/interactive_box/text/exemplar "
            "(point/interactive_box 为 SAM-style 单实例点/框交互, 支持正负点累加 + multimask 候选; "
            "exemplar 为 PCS 全图相似, 支持多正负框累加 + text 概念组合 + per-request 阈值重过滤的"
            "迭代 refinement; bbox 已退役为纯几何形状); "
            "/predict 响应 mask 或 polygon, 平台转换为 polygon 落库。"
        ),
        suggested_backends=(
            SuggestedBackend(
                name="Grounded-SAM-2 (gsam2)",
                repo_url="https://github.com/IDEA-Research/Grounded-SAM-2",
                summary="平台自带参考实现, docker-compose 内置. 文本+框 prompt 开放词汇交互分割。",
                infra="pytorch",
                builtin=True,
            ),
            SuggestedBackend(
                name="SAM3 (sam3)",
                repo_url="https://github.com/facebookresearch/segment-anything",
                summary="平台自带参考实现, docker-compose 内置. Meta 官方 SAM 高质量分割。",
                infra="pytorch",
                builtin=True,
            ),
        ),
    ),
)


# ── 6 个 infra (协议 v2 受控词表) ──────────────────────────────────────
INFRAS: tuple[InfraSpec, ...] = (
    InfraSpec(id="pytorch", label="PyTorch", summary="原生 .pt / TorchScript 部署。"),
    InfraSpec(id="onnx", label="ONNX", summary="跨框架推理, 与硬件解耦。"),
    InfraSpec(
        id="paddle",
        label="Paddle",
        summary="PaddlePaddle / PaddleOCR / PP-Detection 系。",
    ),
    InfraSpec(id="tensorrt", label="TensorRT", summary="NVIDIA GPU 高性能推理。"),
    InfraSpec(id="openvino", label="OpenVINO", summary="Intel CPU / iGPU / NPU 优化。"),
    InfraSpec(id="other", label="其他", summary="协议允许的自定义运行时声明。"),
)


# ── 3 个 modality (与 services/ml_capabilities.derive_modalities 实际输出对齐) ──
# 注: text / point_cloud 是预留语义, 暂未由 derive_modalities 产出, 不进受控表。
MODALITIES: tuple[ModalitySpec, ...] = (
    ModalitySpec(id="image", label="图像", summary="2D 图片 (含视频单帧)。"),
    ModalitySpec(id="video", label="视频", summary="多帧视频序列。"),
    ModalitySpec(
        id="lidar",
        label="点云",
        summary="3D LiDAR 点云 (含 6 相机融合场景帧)。",
    ),
)


# ── geometry (复用 supported_geometric_outputs 字段值) ────────────────────────
GEOMETRIES: tuple[GeometrySpec, ...] = (
    GeometrySpec(id="bbox", label="bbox", summary="2D 轴对齐矩形。"),
    GeometrySpec(
        id="rotated_bbox", label="rotated bbox", summary="带旋转角度的 2D 矩形 (OBB)。"
    ),
    GeometrySpec(
        id="polygon", label="polygon", summary="2D 多边形 (单环或 multi_polygon)。"
    ),
    GeometrySpec(id="polyline", label="polyline", summary="2D 折线。"),
    GeometrySpec(id="keypoint", label="keypoint", summary="2D 关键点集合 (含可见性)。"),
    GeometrySpec(
        id="lidar_box_3d", label="3D box", summary="3D 立体框, 用于点云标注。"
    ),
    GeometrySpec(id="point_mask_3d", label="point mask 3D", summary="点云逐点 mask。"),
    GeometrySpec(id="mask", label="raster mask", summary="2D 逐像素二值掩码。"),
    GeometrySpec(id="none", label="none", summary="纯分类等无几何输出。"),
)


# ── prompt 受控词表 (supported_prompts 合法值) ──────────────────────
# 此前散落在 `ml_capabilities._INTERACTIVE_PROMPTS` 与前端三处常量, 现统一为 SSOT。
# 这是「补登已在协议流通的既有值」(非扩协议), 沿用 既有词表惯例, 不另起 ADR。
PROMPTS: tuple[PromptSpec, ...] = (
    PromptSpec(
        id="none",
        label="无提示",
        requires_input=False,
        interactive_route=False,
        summary="纯批量推理, 无需用户提示 (检测/分类等)。",
    ),
    PromptSpec(
        id="point",
        label="点提示",
        requires_input=True,
        interactive_route=True,
        summary="SAM-style 正/负点累加, 单实例交互分割。",
    ),
    PromptSpec(
        id="interactive_box",
        label="框提示",
        requires_input=True,
        interactive_route=True,
        summary="SAM-style 单框单 mask 交互分割。",
    ),
    PromptSpec(
        id="text",
        label="文本提示",
        requires_input=True,
        interactive_route=False,
        summary="开放词汇文本驱动检测/分割; 走批量线, 不进画布交互工具。",
    ),
    PromptSpec(
        id="exemplar",
        label="视觉示例",
        requires_input=True,
        interactive_route=True,
        summary="PCS 框样例找全图同类, 多正负框 + text 概念 + 阈值迭代 refinement。",
    ),
    PromptSpec(
        id="scribble",
        label="涂抹",
        requires_input=True,
        interactive_route=True,
        summary="涂抹笔迹提示 (预留, 暂无 backend 消费)。",
    ),
    PromptSpec(
        id="sketch",
        label="勾画",
        requires_input=True,
        interactive_route=True,
        summary="勾画轮廓提示 (预留, 暂无 backend 消费)。",
    ),
    PromptSpec(
        id="mask",
        label="掩膜",
        requires_input=True,
        interactive_route=True,
        summary="已有原生 mask 作提示继续细化。",
    ),
    PromptSpec(
        id="correction_frame",
        label="纠错帧",
        requires_input=True,
        interactive_route=False,
        summary="以人工纠错关键帧为种子执行有界视频重传播。",
    ),
    PromptSpec(
        id="bbox",
        label="bbox (退役)",
        requires_input=True,
        interactive_route=False,
        summary="旧单框提示, 已被 interactive_box 取代; 仅兼容历史快照。",
    ),
)


# ── input 受控词表 (supported_inputs 合法值) ─────────────────────────
INPUTS: tuple[InputSpec, ...] = (
    InputSpec(id="full_image", label="整图", summary="整张图片或视频帧作为模型输入。"),
    InputSpec(id="crop", label="裁剪", summary="上游几何裁出的 ROI 图像。"),
    InputSpec(
        id="bbox_prompt", label="框提示", summary="整图 + 上游 bbox/polygon 框提示。"
    ),
    InputSpec(id="point_prompt", label="点提示", summary="整图 + 点提示。"),
    InputSpec(
        id="mask_prompt", label="Mask 提示", summary="整图或视频 + 已有原生 Mask 提示。"
    ),
    InputSpec(
        id="scribble_prompt", label="涂抹提示", summary="整图 + 正负二值涂抹笔迹。"
    ),
    InputSpec(id="video", label="视频", summary="多帧视频序列输入。"),
)


# ── 派生表 (供 ml_capabilities.py 消费, 避免双份维护) ───────────────────────────
INFRA_VALUES: tuple[str, ...] = tuple(s.id for s in INFRAS)
TASKS = tuple(sorted(TASKS, key=lambda spec: CANONICAL_TASK_VALUES.index(spec.id)))
TASK_VALUES = CANONICAL_TASK_VALUES
GEOMETRY_VALUES: tuple[str, ...] = tuple(s.id for s in GEOMETRIES)
MODALITY_VALUES: tuple[str, ...] = tuple(s.id for s in MODALITIES)
PROMPT_VALUES: tuple[str, ...] = tuple(s.id for s in PROMPTS)
INPUT_VALUES: tuple[str, ...] = tuple(s.id for s in INPUTS)
INPUT_FULL_IMAGE = "full_image"
INPUT_CROP = "crop"
INPUT_BBOX_PROMPT = "bbox_prompt"
INPUT_POINT_PROMPT = "point_prompt"
INPUT_MASK_PROMPT = "mask_prompt"
INPUT_SCRIBBLE_PROMPT = "scribble_prompt"
INPUT_VIDEO = "video"
# requires_input 集合 = 旧 _INTERACTIVE_PROMPTS (含 text/bbox); interactive_route 集合 =
# 旧前端 INTERACTIVE_PROMPTS 的超集 (含预留 scribble/sketch/mask, 不含 text)。
PROMPTS_REQUIRES_INPUT: frozenset[str] = frozenset(
    s.id for s in PROMPTS if s.requires_input
)
PROMPTS_INTERACTIVE_ROUTE: frozenset[str] = frozenset(
    s.id for s in PROMPTS if s.interactive_route
)

TASK_DEFAULT_GEOMETRY: dict[str, list[str]] = {
    s.id: list(s.default_geometry) for s in TASKS
}
