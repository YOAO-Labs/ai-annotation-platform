from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from aap_protocol_v2.lifecycle import (
    ManagedLifecycleCapabilities,
    canonical_managed_lifecycle_capabilities,
)
from aap_inference import ModelCapability
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from aap_task_contract import normalize_modality_fields

from app.utils.gpu_resource import validate_gpu_resource_id


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
_POSTGRES_INT_MIN = -(2**31)
_POSTGRES_INT_MAX = 2**31 - 1

GPUResourceId = Annotated[
    str,
    Field(strict=True, min_length=3, max_length=512),
]
VRAMBudgetMB = Annotated[
    int,
    Field(strict=True, ge=1, le=_POSTGRES_INT_MAX),
]
EvictionPriority = Annotated[
    int,
    Field(strict=True, ge=_POSTGRES_INT_MIN, le=_POSTGRES_INT_MAX),
]


def _validate_ml_backend_url(v: str) -> str:
    """v0.9.8 · 拒绝 loopback host. 容器内无法访问宿主机 localhost,
    跑预标会直接 connection refused. 提示用 docker bridge IP / service DNS.

    与 v0.9.6 前端 placeholder (runtime-hints.ml_backend_default_url) 配套.
    """
    parsed = urlparse(v)
    host = (parsed.hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        raise ValueError(
            "URL 不能用 loopback host (localhost / 127.0.0.1); "
            "容器内访问宿主机请用 docker bridge IP (如 172.17.0.1) 或 service DNS. "
            "默认值参考 GET /runtime-hints.ml_backend_default_url"
        )
    return v


def _validate_gpu_resource_id(value: str) -> str:
    return validate_gpu_resource_id(value)


class MLBackendCreate(BaseModel):
    name: str
    url: str
    is_interactive: bool = False
    auth_method: str = "none"
    auth_token: str | None = None
    extra_params: dict = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def _no_loopback(cls, v: str) -> str:
        return _validate_ml_backend_url(v)


class MLBackendUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    is_interactive: bool | None = None
    auth_method: str | None = None
    auth_token: str | None = None
    extra_params: dict | None = None

    @field_validator("url")
    @classmethod
    def _no_loopback(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_ml_backend_url(v)


class MLBackendRegistryCreate(MLBackendCreate):
    """Superadmin-only global registry payload with a strong-typed GPU claim."""

    model_config = ConfigDict(extra="forbid")

    gpu_resource_id: GPUResourceId | None = None
    vram_budget_mb: VRAMBudgetMB | None = None
    eviction_priority: EvictionPriority = 0

    @field_validator("gpu_resource_id")
    @classmethod
    def _single_gpu_resource(cls, value: str | None) -> str | None:
        return _validate_gpu_resource_id(value) if value is not None else None

    @model_validator(mode="after")
    def _complete_gpu_claim(self):
        if (self.gpu_resource_id is None) != (self.vram_budget_mb is None):
            raise ValueError(
                "gpu_resource_id 与 vram_budget_mb 必须同时设置或同时为 null"
            )
        return self


class MLBackendRegistryUpdate(MLBackendUpdate):
    """Superadmin-only partial update; the service validates the merged claim."""

    model_config = ConfigDict(extra="forbid")

    gpu_resource_id: GPUResourceId | None = None
    vram_budget_mb: VRAMBudgetMB | None = None
    eviction_priority: EvictionPriority | None = None

    @field_validator("gpu_resource_id")
    @classmethod
    def _single_gpu_resource(cls, value: str | None) -> str | None:
        return _validate_gpu_resource_id(value) if value is not None else None

    @model_validator(mode="after")
    def _priority_cannot_be_cleared(self):
        if (
            "eviction_priority" in self.model_fields_set
            and self.eviction_priority is None
        ):
            raise ValueError("eviction_priority 不得为 null")
        return self


# v0.9.11 PerfHud · health_meta 子结构 (Pydantic 模型 → 前端 codegen 派生类型)
class GpuInfo(BaseModel):
    device_name: str | None = None
    device_index: int | None = None
    device_uuid: str | None = None
    mig_uuid: str | None = None
    physical_device_token: str | None = None
    memory_used_mb: int | None = None
    memory_total_mb: int | None = None
    memory_free_mb: int | None = None
    process_memory_mb: int | None = None
    gpu_utilization_percent: int | None = None
    gpu_temperature_celsius: int | None = None
    gpu_power_watts: float | None = None

    class Config:
        extra = "allow"


class HostInfo(BaseModel):
    container_cpu_percent: float | None = None
    container_memory_percent: float | None = None


class CacheStats(BaseModel):
    hits: int | None = None
    misses: int | None = None
    size: int | None = None
    capacity: int | None = None
    hit_rate: float | None = None

    class Config:
        extra = "allow"  # backend 可能扩展指标, 不强约束


class CapabilityWarning(BaseModel):
    """v0.18.29 · backend `/setup` 上报值的受控词表校验诊断 (越界即记, 不阻断解析)。

    由 services/ml_capabilities._collect_warnings 产出; 前端模型市场据此显示 ⚠ +
    可读原因, 把「字段拼错 / 值越界致工具静默不亮」变成可见信号。"""

    level: str = "warning"  # warning | info
    model_id: str | None = None
    field: str
    value: str
    message: str


class BackendCapabilities(BaseModel):
    """v0.10.37 · backend `/setup` 能力快照 (epic 阶段 1).

    由 services/ml_backend.check_health 探 `/setup` 后落进 health_meta["capabilities"];
    `modalities` 为派生视图 (image / video / lidar), 见 services/ml_capabilities.derive_modalities.

    v0.14.9 · 能力声明协议 v2: 新增 `infra` (backend 默认基础设施) + `models` (多模型目录);
    其余顶层字段为各 model 的「扁平并集」, 供未迁移消费方继续读 (向后兼容)。"""

    # v0.14.12 · backend 自报名 (如 "grounded-sam2-backend"), 用于前端能力目录展示
    # 源 backend 名 (而非用户在项目里取的别名)。
    name: str | None = None
    version: str | None = None
    protocol_version: str = "1"
    task_contract_revision: Literal[0, 1] = 0
    compat_protocol_versions: list[str] = []
    model_version: str | None = None
    weights_version: str | None = None
    # v0.14.9 · 协议 v2 新增
    infra: str = "unknown"
    # v0.14.14 · backend 是否支持 POST /warmup (协议 §4.4). 老 backend 缺字段 = False.
    warmup_endpoint: bool = False
    # ADR-0049 · strict canonical `/setup.managed_lifecycle`; None means the
    # backend cannot participate in enforce promotion.
    managed_lifecycle: ManagedLifecycleCapabilities | None = None

    @field_validator("managed_lifecycle", mode="before")
    @classmethod
    def validate_managed_lifecycle(cls, value: object) -> object:
        if value is None:
            return None
        return canonical_managed_lifecycle_capabilities(value)

    models: list[ModelCapability] = []
    # 扁平并集 (向后兼容)
    is_interactive: bool = False
    supported_prompts: list[str] = []
    supported_inputs: list[str] = []
    supported_trackers: list[str] = []
    # v0.21.19 · text-driven tracker (sam3_video) 子集; propagate 需 text/exemplars。
    text_driven_trackers: list[str] = []
    supported_text_outputs: list[str] = []
    supported_geometric_outputs: list[str] = []
    modalities: list[str] = []
    # v0.18.29 · 受控词表校验诊断 (越界 task/infra/prompt/geometry); 空 = 全合法。
    warnings: list[CapabilityWarning] = []


class ComputeInfo(BaseModel):
    """Backend 有效计算设备观测 (五镜像统一)。

    torch 系 (yolo/gsam2/sam3) 填 ``effective_device`` ("cuda"/"cpu"/None),
    ORT 系 (rapidocr/onnxtools) 填 ``effective_provider`` (如 "CPUExecutionProvider"/
    "CUDAExecutionProvider")。``configured_device`` 为配置意图或构造偏好; 只有已知 GPU
    配置 ("gpu"/"cuda"/"cuda:*") 的有效设备已落到 CPU 时, 前端才显示「⚠ CPU 回退」。
    该对象仅作诊断，不代表所有 GPU pool/session 已释放。
    """

    configured_device: str | None = None
    effective_device: str | None = None
    effective_provider: str | None = None
    # False 表示 GPU-only，设备失效时必须显式失败，不得展示 CPU fallback。
    cpu_fallback_supported: bool | None = None

    class Config:
        extra = "allow"


class PoolResidencyInfo(BaseModel):
    resident: bool | None = None
    device: str | None = None
    provider: str | None = None

    class Config:
        extra = "allow"


class ResidencyIdentityInfo(BaseModel):
    audience: str | None = None
    backend_registry_id: str | None = None
    gpu_resource_id: str | None = None

    class Config:
        extra = "allow"


class ResidencyInfo(BaseModel):
    """Backend-reported physical GPU residency; independent from compute intent."""

    state: Literal[
        "unloaded", "loading", "resident", "draining", "unloading", "unknown"
    ]
    gpu_loaded: bool | None = None
    active_requests: int | None = None
    builders: int | None = None
    borrowers: int | None = None
    draining: bool | None = None
    evictable: bool | None = None
    generation: str | None = None
    pools: dict[str, PoolResidencyInfo] = Field(default_factory=dict)
    boot_id: str | None = None
    lifecycle_gate: Literal["legacy", "enforce"] | None = None
    control_epoch: str | None = None
    identity: ResidencyIdentityInfo | None = None

    class Config:
        extra = "allow"


class MLBackendUnloadResponse(BaseModel):
    """跨 backend 兼容的 legacy `/unload` 响应；额外字段保持透传。"""

    ok: bool | None = None
    unloaded: bool | int | None = None
    residency: ResidencyInfo | dict[str, Any] | None = None

    model_config = ConfigDict(extra="allow")


class GPUConfigDiagnostic(BaseModel):
    code: str
    level: Literal["info", "warning", "critical", "blocker"]
    message: str
    field: str | None = None
    resource_id: str | None = None
    backend_id: UUID | None = None


GPURolloutState = Literal[
    "disabled",
    "uninitialized",
    "off",
    "promoting",
    "enforcing",
    "demoting",
    "blocked",
]


class GPUBackendConfigStatus(BaseModel):
    status: Literal["ok", "info", "warning", "critical", "blocker"] = "ok"
    desired_mode: Literal["off", "observe", "enforce"] = "off"
    effective_mode: Literal["off", "observe", "enforce"] = "off"
    rollout_enabled: bool | None = None
    rollout_state: GPURolloutState | None = None
    rollout_revision: int | None = None
    rollout_blocker_reason: str | None = None
    allocatable_mb: int | None = None
    resource_claimed_budget_mb: int | None = None
    diagnostics: list[GPUConfigDiagnostic] = Field(default_factory=list)


class GPUConfigErrorDetail(BaseModel):
    error_code: Literal["gpu_config_invalid"] = "gpu_config_invalid"
    message: str
    diagnostics: list[GPUConfigDiagnostic] = Field(default_factory=list)


class GPUConfigErrorResponse(BaseModel):
    detail: GPUConfigErrorDetail


class RequestValidationErrorResponse(BaseModel):
    """FastAPI's default request validation envelope for non-GPU fields."""

    detail: list[dict[str, Any]]


class MLBackendRegistryConflictDetail(BaseModel):
    error_code: Literal[
        "ml_backend_url_conflict",
        "gpu_backend_active",
        "gpu_backend_retirement_required",
    ]
    message: str
    active_workloads: int | None = None


class MLBackendRegistryConflictResponse(BaseModel):
    detail: MLBackendRegistryConflictDetail


class HealthMeta(BaseModel):
    """v0.9.11 · backend `/health` 深度指标缓存. 由 services/ml_backend.check_health 写入,
    `/admin/ml-integrations/overview` + PerfHud WS 消费."""

    gpu_info: GpuInfo | None = None
    host: HostInfo | None = None
    cache: CacheStats | None = None
    model_version: str | None = None
    # v0.10.37 · /setup 能力快照 (epic 阶段 1)
    capabilities: BackendCapabilities | None = None
    # v0.22.3 WS4 · 有效计算设备观测 (GPU 静默退回 CPU 告警用)。
    compute: ComputeInfo | None = None
    # ADR-0049 · 真实 pool/session GPU 驻留，不得由 compute=cpu 推断。
    residency: ResidencyInfo | dict[str, Any] | None = None

    class Config:
        extra = "allow"


class MLBackendOut(BaseModel):
    id: UUID
    # v0.19.0 ADR-0044 · backend 上提为全局注册项, 无项目归属; 项目作用域端点从路径注入
    # 本项目 id (表「该项目启用了此全局 backend」), 全局/admin 端点留 None。
    project_id: UUID | None = None
    name: str
    url: str
    state: str
    is_interactive: bool
    auth_method: str
    extra_params: dict
    gpu_resource_id: str | None = None
    vram_budget_mb: int | None = None
    eviction_priority: int = 0
    # v0.9.6 · 缓存的 backend `/health` 深度指标 (gpu_info / cache / model_version);
    # 由 services/ml_backend.check_health 写入, /admin/ml-integrations/overview 直接消费.
    # v0.9.11 · 类型从 dict 收紧到 HealthMeta (含 host PerfHud 字段), 前端 codegen 派生.
    health_meta: HealthMeta | dict[str, Any] | None = None
    error_message: str | None
    last_checked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# v0.9.11 PerfHud · WS /ws/ml-backend-stats 推送的单次快照
class MLBackendStatsBinding(BaseModel):
    backend_id: UUID
    backend_name: str
    project_id: UUID | None = None
    project_display_id: str | None = None
    project_name: str | None = None


class MLBackendStatsSnapshot(BaseModel):
    physical_key: str | None = None
    url_host: str | None = None
    backend_id: UUID
    backend_name: str | None = None
    bindings: list[MLBackendStatsBinding] = Field(default_factory=list)
    state: str
    gpu_info: GpuInfo | None = None
    host: HostInfo | None = None
    cache: CacheStats | None = None
    model_version: str | None = None
    loaded: bool | None = None
    idle_unload_seconds: float | None = None
    last_request_age_seconds: float | None = None
    pool: dict | None = None
    video_pool: dict | None = None
    # v0.22.3 WS4 · 有效计算设备观测 (GPU 静默退回 CPU 告警用)。
    compute: ComputeInfo | None = None
    residency: ResidencyInfo | dict[str, Any] | None = None
    timestamp: datetime


class MLBackendHealthResponse(BaseModel):
    status: str
    backend_id: UUID
    backend_name: str


# v0.19.0 ADR-0044 · 项目「启用勾选清单」: 列出全部全局 backend + 本项目启用态/覆盖。
class ProjectMLBackendItem(BaseModel):
    """一行 = 一个全局 backend 在本项目的启用态 + 项目级覆盖。

    `backend` 是全局注册项快照 (project_id=None); `enabled`/`default_variants` 来自
    project_ml_backend 关联 (无关联则 enabled=False、覆盖 None)。"""

    backend: MLBackendOut
    enabled: bool = False
    default_variants: dict | None = None


class ProjectMLBackendList(BaseModel):
    items: list[ProjectMLBackendItem]


class ProjectMLBackendEnablement(BaseModel):
    """切换项目启用 + 写项目级变体覆盖。覆盖项缺省 = 不改动。"""

    enabled: bool
    default_variants: dict | None = None


# v0.23.3 ADR-0050 §12.2 · 项目服务池绑定 API (pool-level)。
class MLBackendPoolSummary(BaseModel):
    """服务池摘要 (项目可用清单 / 启用态)。

    v0.23.3 off/observe: 每池是 singleton, legacy_instance 指向唯一 registry 实例;
    项目设置勾选清单读此。完整池管理 (成员 / 权重 / drain) 留给 v0.23.4 超管 UI。"""

    id: UUID
    name: str
    enabled: bool = False
    legacy_instance_id: UUID | None = None
    member_count: int = 0
    routing_generation: int = 1


class ProjectMLBackendPoolItem(BaseModel):
    """一行 = 一个服务池在本项目的启用态 + 项目级变体覆盖 (pool 级)。"""

    pool: MLBackendPoolSummary
    enabled: bool = False
    default_variants: dict | None = None


class ProjectMLBackendPoolList(BaseModel):
    items: list[ProjectMLBackendPoolItem]


class ProjectMLBackendPoolEnablement(BaseModel):
    """切换项目对某服务池的启用 + 写项目级变体覆盖 (pool 级)。"""

    enabled: bool
    default_variants: dict | None = None


# v0.10.26 · 模型市场单变体预热. 缺省时 backend 用默认变体 (保持旧 /reload 行为).
class MLBackendReloadRequest(BaseModel):
    sam_variant: str | None = None
    dino_variant: str | None = None
    # v0.10.36 · "image" 预热图片池 (默认), "video" 预热独立 video tracker 池 (仅认 sam_variant)
    modality: Literal["image", "video"] | None = None

    @model_validator(mode="before")
    @classmethod
    def canonical_modality_fields(cls, value):
        return normalize_modality_fields(value)


class InteractiveRequest(BaseModel):
    """工作台「AI 助手」单次推理请求。

    兼容请求的 ``context`` 仍是开放 dict；显式请求原生 Mask 时，平台会按目标
    model 能力预检、重建 prompt revision，并严格校验 backend 候选与字节上限。

    `context.type` 协商枚举（详见 `docs-site/dev/ml-backend-protocol.md` §2.2）：
    - ``point``：``{"type":"point","points":[[x,y],...],"labels":[1,0,...],"multimask_output":false}``
      （v0.18.17 · SAM-style 单实例点交互, 正/负点累加由前端重发全量点; multimask 出 3 候选）
    - ``interactive_box``：``{"type":"interactive_box","bbox":[x1,y1,x2,y2],"multimask_output":false}``
      （v0.18.17 · SAM-style 单框单 mask; 双 backend 统一名, 旧 ``bbox`` prompt 已退役）
    - ``polygon``：``{"type":"polygon","points":[[x,y],...]}``
    - ``text``：``{"type":"text","text":"ripe apples"}``（Grounded-SAM-2 DINO / SAM 3 PCS）
    - ``exemplar``：``{"type":"exemplar","bbox":[x1,y1,x2,y2]}`` 或多正负框
      ``{"type":"exemplar","exemplars":[{"bbox":[...],"label":true},...],"text":"car","score_threshold":0.5}``
      （SAM 3 PCS 全图相似实例; v0.18.19 起支持多正负框累加 + text 概念组合 + 阈值重过滤的迭代 refinement）。
    """

    task_id: UUID
    context: dict = Field(
        default_factory=dict,
        description="开放 dict；type 字段见 schema docstring 与协议文档 §2.2。",
    )


class InteractiveRoutingLineage(BaseModel):
    requested_backend_id: UUID
    backend_pool_id: UUID | None = None
    backend_instance_id: UUID | None = None
    model_id: str | None = None


class InteractiveMaskDiagnostic(BaseModel):
    reason: str
    retryable: bool = False
    message: str | None = None
    supported_geometric_outputs: list[str] | None = None


class InteractiveAnnotateResponse(BaseModel):
    """Shared image/current-frame interactive response, including native Mask lineage."""

    result: list[dict[str, Any]] = Field(default_factory=list)
    score: float | None = None
    model_version: str | None = None
    inference_time_ms: float | None = None
    cache_hit: bool | None = None
    model_load_ms: float | None = None
    mask_input_next: str | None = None
    diagnostic: InteractiveMaskDiagnostic | None = None
    prompt_revision: str | None = None
    output_geometry: Literal["polygon", "mask"] = "polygon"
    frame_index: int | None = None
    routing: InteractiveRoutingLineage
    prompt_summary: dict[str, Any] | None = None
    accept_receipts: dict[str, str] = Field(default_factory=dict)
