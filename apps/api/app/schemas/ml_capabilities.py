"""v0.14.11 · 协议级能力目录响应 schema.

承载 `GET /v1/ml-capabilities/protocol` 的 pydantic 模型。与
`services/capability_registry.py` 的 dataclass 一一对应; 前端 codegen 派生类型。
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from aap_task_contract import normalize_task
from pydantic import field_validator


class SuggestedBackendItem(BaseModel):
    name: str
    repo_url: str
    summary: str
    research_link: str | None = None
    infra: str | None = None  # 与受控 INFRAS 对齐 (pytorch / onnx / ...)
    builtin: bool = False  # True = 平台 docker-compose 自带; False = 外部推荐


class ProtocolTaskItem(BaseModel):
    id: str
    label: str
    summary: str
    default_geometry: list[str]
    default_modalities: list[str]
    typical_models: list[str]
    protocol_notes: str
    suggested_backends: list[SuggestedBackendItem] = []


class ProtocolInfraItem(BaseModel):
    id: str
    label: str
    summary: str


class ProtocolModalityItem(BaseModel):
    id: str
    label: str
    summary: str


class ProtocolGeometryItem(BaseModel):
    id: str
    label: str
    summary: str


class ProtocolPromptItem(BaseModel):
    # v0.18.30 · prompt 受控词表对外暴露 (供前端 codegen + 运行时同源)。
    # requires_input/interactive_route 双维度见 capability_registry.PromptSpec。
    id: str
    label: str
    summary: str
    requires_input: bool
    interactive_route: bool


class ProtocolInputItem(BaseModel):
    # v0.21.0 · supported_inputs 受控词表对外暴露 (供前端 codegen + 判据同源)。
    id: str
    label: str
    summary: str


class InstanceVariantOption(BaseModel):
    """variants axis 内一条选项 (透传自 backend /setup; 用于前端列表按 axis 拆行)."""

    value: str
    label: str | None = None
    vram_gb: float | None = None
    tier: str | None = None
    recommended: bool = False
    note: str | None = None


class InstanceVariantGroup(BaseModel):
    """variants 多轴的单根轴 (序列保持与 backend /setup 顺序一致)."""

    key: str
    title: str | None = None
    description: str | None = None
    variants: list[InstanceVariantOption] = []


class InstanceModelItem(BaseModel):
    """实例级 model 视图 (字段裁剪自协议 v2 ModelCapability, 不暴露 url / params 等运维
    信息; v0.14.12 起补 supported_variants + variant_combinations 让前端能按主轴展开
    「具体模型」行; v0.19.2 WS0 起补 supported_inputs + resource_profile, 与项目级
    /capabilities 对齐, 供全局编排选择器消费投递契约/批量画像)."""

    id: str
    display_name: str
    task: str = "unknown"

    @field_validator("task", mode="before")
    @classmethod
    def canonical_task(cls, value):
        return normalize_task(value, allow_unknown=True)

    model_family: str | None = None
    infra: str | None = None
    is_interactive: bool = False
    supported_prompts: list[str] = []
    # v0.19.2 WS0 · 一等输入契约 (full_image|crop|bbox_prompt|point_prompt) + 资源画像
    # (device / batchable)。原 /instances 裁掉了二者, 全局编排选择器需要。
    supported_inputs: list[str] = []
    # v0.21.0 · 源阶段默认输入类型; backend 未声明时由 supported_inputs[0] 兜底。
    default_input_type: str | None = None
    resource_profile: dict = {}
    supported_geometric_outputs: list[str] = []
    supported_trackers: list[str] = []
    # v0.21.19 · text-driven tracker (sam3_video) 子集; 前端选中该 tracker 时显 text 框。
    text_driven_trackers: list[str] = []
    max_window_frames: int | None = Field(default=None, gt=0)
    # 协议③ · 属性输出类型 + schema 自描述 (含 select options), 供「从 backend 导入属性」.
    output_attribute_types: list[str] = []
    output_attribute_schema: list[dict] = []
    # v0.20.3 · backend 自报的类别清单 (yolo COCO 等)。供前端「从 backend 预填配置」一键
    # 导入类别——此前本字段在 /instances 被裁掉, 类别只能手抄。缺省 [] 向后兼容。
    # 形态与 MLModelCapability.classes (ml_backend.py) 一致: [{index,name}], 不是 string[] ——
    # 此前误标 list[str] 导致任何自报非空 classes 的 backend (yolo/onnxtools) 校验 80+ 个
    # ValidationError, 整条 backend 被 /instances 路由层静默 catch 掉, 能力目录整体丢失该 backend。
    classes: list[dict] = []
    modality: str | None = None
    supported_variants: list[InstanceVariantGroup] = []
    variant_combinations: list[list[str]] = []
    # v0.14.12 · True 表示同 backend 内多个 task 共享同一份物理权重 (如 gsam2 的 SAM2
    # 权重同时被 segmentation / interactive_seg / tracker 用). 前端列表据此把跨 task
    # 的同 variant 合并到一行, 而不是为每个 task 重复显示。yolo 每 task 独立权重
    # (yolov8n-det.pt vs yolov8n-obb.pt), 缺省 False 保持「每 task 一行 + 任务后缀」。
    variants_shared_across_tasks: bool = False
    # v0.14.13 · backend 自报的默认 variant 组合 (dict[axis_key, value]).
    # 前端 VariantSelector 在用户未选时取此作初值; 优先级链:
    # 项目级 project.default_variants[backend_id] > 本字段 > backend 启动 env 默认.
    default_variants: dict[str, str] = {}
    # 协议 v2.2 · 原子 vs 内部编排维度：atom=单次推理原子；composite=内部编排多原子。
    # 缺省 atom 向后兼容。编排下游 stage 据此过滤（只组合 atom）。
    composition: str = "atom"


class CapabilityInstanceItem(BaseModel):
    """平台已知 backend 的实例视图 (env-only 容器 + 项目级注册合并)。

    `backend_id` 是 ml_backend_registry.id, 供全局编排选择器落 pipeline_stages.ml_backend_id。
    `state` 保留 connected/error 等注册表状态; disconnected 已在 service 层过滤。
    `source` 直接透传 ml_backend_registry.source: "env" = docker-compose/运维 env 配的容器
              (启动钩子按 env URL 自动 upsert), "manual" = superadmin 在模型市场手动注册。
    `name` 是 backend 显示名 (ml_backend_registry.name)。
    不暴露 url / project_id / health 指标, 让普通登录用户安全消费。
    """

    backend_id: str
    state: str
    source: str
    name: str
    infra: str = "unknown"
    # v0.14.14 · backend 是否支持 POST /warmup (协议 §4.4); 前端模型市场"⚡ 预热"
    # 按钮据此置灰. 老 backend 缺字段 = False.
    warmup_endpoint: bool = False
    models: list[InstanceModelItem] = []


class CapabilityInstancesResponse(BaseModel):
    instances: list[CapabilityInstanceItem]


class ProtocolCapabilitiesResponse(BaseModel):
    """协议能力目录响应顶层结构。

    `version` 与 ml-backend-protocol 协议版本对齐 (当前 v2); 受控词表的不兼容
    变更才 bump (新增 task 不算)。
    """

    version: str
    task_aliases: dict[str, str] = Field(default_factory=dict)
    tasks: list[ProtocolTaskItem]
    infras: list[ProtocolInfraItem]
    modalities: list[ProtocolModalityItem]
    geometries: list[ProtocolGeometryItem]
    # v0.18.30 · prompt 受控词表 (第五张; 此前仅内部消费, 现对外暴露供前端 codegen)。
    prompts: list[ProtocolPromptItem] = []
    # v0.21.0 · input 受控词表 (第六张; supported_inputs 合法值)。
    inputs: list[ProtocolInputItem] = []
