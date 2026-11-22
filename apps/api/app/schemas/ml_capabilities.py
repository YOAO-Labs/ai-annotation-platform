"""协议级能力目录响应 schema.

承载 `GET /v1/ml-capabilities/protocol` 的 pydantic 模型。与
`services/capability_registry.py` 的 dataclass 一一对应; 前端 codegen 派生类型。
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from yoao_task_contract import normalize_task
from pydantic import field_validator


class SuggestedBackendItem(BaseModel):
    name: str
    repo_url: str
    summary: str
    research_link: str | None = None
    infra: str | None = None
    builtin: bool = False


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
    id: str
    label: str
    summary: str
    requires_input: bool
    interactive_route: bool


class ProtocolInputItem(BaseModel):
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
    信息)。supported_variants 和 variant_combinations 用于按主轴展开具体模型；
    supported_inputs 和 resource_profile 与项目级 /capabilities 对齐，
    供全局编排选择器读取输入契约和批量执行能力。"""

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
    supported_inputs: list[str] = []
    default_input_type: str | None = None
    resource_profile: dict = {}
    supported_geometric_outputs: list[str] = []
    supported_trackers: list[str] = []
    text_driven_trackers: list[str] = []
    max_window_frames: int | None = Field(default=None, gt=0)
    output_attribute_types: list[str] = []
    output_attribute_schema: list[dict] = []
    classes: list[dict] = []
    modality: str | None = None
    supported_variants: list[InstanceVariantGroup] = []
    variant_combinations: list[list[str]] = []
    variants_shared_across_tasks: bool = False
    default_variants: dict[str, str] = {}
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
    prompts: list[ProtocolPromptItem] = []
    inputs: list[ProtocolInputItem] = []
