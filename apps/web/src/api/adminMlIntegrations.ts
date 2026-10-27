import { apiClient } from "./client";
import type { MLBackendSupportedVariantGroup } from "./ml-backends";
import type {
  GPUArbiterResourcesResponse,
  CapabilityDriftAcceptRequest,
  CapabilityDriftPreviewResponse,
  GPUBackendConfigStatus,
  GpuInfo,
  MLBackendUnloadResponse,
  ResidencyInfo,
  // v0.23.4 · ML routing read models + service-pool CRUD (ADR-0050).
  TopologyResponse,
  RuntimeSnapshotResponse,
  ServicePoolAdminItem,
  ServicePoolCreateRequest,
  ServicePoolPatchRequest,
  ServicePoolMemberPutRequest,
} from "./generated/types.gen";
import type { MLBackendCompute } from "@/utils/mlBackendCompute";

export type {
  GPUArbiterResourceItem,
  GPUArbiterResourcesResponse,
  CapabilityDriftAcceptRequest,
  CapabilityDriftPreviewResponse,
  GPUBackendConfigStatus,
  GPUConfigDiagnostic,
  GpuInfo,
  MLBackendUnloadResponse,
  ResidencyInfo,
  // v0.23.4 · ML routing read models + service-pool CRUD (ADR-0050).
  TopologyResponse,
  TopologyPoolEntry,
  TopologyMemberInstance,
  RuntimeSnapshotResponse,
  RuntimePoolSnapshot,
  RuntimeMemberSnapshot,
  SourceFreshness,
  ServicePoolAdminItem,
  ServicePoolMemberItem,
  ServicePoolCreateRequest,
  ServicePoolPatchRequest,
  ServicePoolMemberPutRequest,
} from "./generated/types.gen";

export type { BucketSummary } from "./storage";
export type StorageOverview = Pick<
  import("./storage").BucketsResponse,
  "items" | "total_object_count" | "total_size_bytes"
>;

// v0.10.26 · 单变体 cache 桶 (来自 backend /cache/stats，key="sam/dino")。
export interface CacheBucketStat {
  size?: number;
  hits?: number;
  misses?: number;
  hit_rate?: number;
}

// v0.14.14 · 协议 §4.3 PoolStatus.loaded_keys 元数据 (loaded_at/last_used_at 为 ISO).
export interface PoolLoadedKey {
  key: string;
  loaded_at?: string;
  last_used_at?: string;
  hit_count?: number;
}

// v0.14.14 · 协议 §4.3 last_evict.
export interface PoolEvictRecord {
  key: string;
  at: string;
  reason: string;
}

// v0.10.26 · ModelPool 健康快照 (来自 backend /health.pool).
// v0.14.14 起 backend 改发 PoolStatus 字段 (cap/current_size/loaded_keys/last_evict);
// 老字段 loaded_variants/evict_count/per_variant_lru_ts 在 gsam2 双发期内并存,
// 消费方优先读新字段, 老字段仅作 fallback (本仓库 backend 升级到 v0.14.14 后可清).
export interface BackendPoolMeta {
  cap?: number;
  current_size?: number;
  loaded_keys?: PoolLoadedKey[];
  last_evict?: PoolEvictRecord | null;
  // 下三个字段是 v0.14.14 协议前的老字段, gsam2 双发期内并存; 消费方应优先读
  // loaded_keys / last_evict, 这里保留仅为 fallback. 注: 不用 jsdoc @deprecated tag,
  // 避免触发 TS6385/6387 在 fallback 调用点污染警告 (sessionVariantCache 同款理由).
  loaded_variants?: Array<{ sam_variant: string; dino_variant: string }>;
  evict_count?: number;
  per_variant_lru_ts?: Record<string, number>;
}

// v0.10.36 · video tracker 独立显存池快照 (来自 backend /health.video_pool).
// v0.14.14 起同样上报 PoolStatus loaded_keys; video pool 的 key 就是 sam_variant 字符串.
export interface VideoPoolMeta {
  cap: number;
  current_size?: number;
  loaded_keys?: PoolLoadedKey[];
  last_evict?: PoolEvictRecord | null;
  // 老字段, 同 BackendPoolMeta.loaded_variants 说明; 优先读 loaded_keys.
  loaded_variants: string[];
  active_sessions: number;
  idle_seconds?: number;
}

export interface BackendHealthMeta {
  gpu_info?: GpuInfo | null;
  cache?: {
    hit_rate?: number;
    hits?: number;
    misses?: number;
    buckets?: Record<string, CacheBucketStat>;
    [key: string]: unknown;
  } | null;
  model_version?: string | null;
  /** v0.10.26 · ModelPool 多变体并存快照 (grounded-sam2 才有)。 */
  pool?: BackendPoolMeta | null;
  /** v0.10.36 · video tracker 独立显存池快照 (支持视频追踪的 backend 才有)。 */
  video_pool?: VideoPoolMeta | null;
  /** v0.10.37 · /setup 能力快照 (epic 阶段 1); check_health 探 /setup 后落库。 */
  capabilities?: BackendCapabilities | null;
  /** v0.22.3 WS4 · 有效计算设备观测 (GPU 静默退回 CPU 告警用)。 */
  compute?: MLBackendCompute | null;
  /** ADR-0049 · 全 pool/session 的物理 GPU 驻留真值；第三方旧响应允许保留原始对象。 */
  residency?: ResidencyInfo | Record<string, unknown> | null;
}

// v0.10.37 · backend /setup 能力快照 (epic 阶段 1); modalities 为派生视图 (image/video)。
export interface BackendCapabilities {
  is_interactive: boolean;
  supported_prompts: string[];
  supported_trackers: string[];
  supported_text_outputs: string[];
  supported_geometric_outputs: string[];
  modalities: string[];
  // v0.14.9 协议 v2: 多 model 目录 (yolo 一个进程暴露 detection/segmentation/keypoint/obb
  // 4 个 task model). gsam2/sam3 是单 model, 此处缺省或长度 0.
  models?: Array<{ id: string; task?: string }>;
  // v0.14.14: backend 自报支持 POST /warmup (协议 §4.4); 老 backend 缺字段 = false.
  warmup_endpoint?: boolean;
}

export interface MLBackendItem {
  id: string;
  project_id: string;
  name: string;
  url: string;
  state: string;
  is_interactive: boolean;
  auth_method: string;
  extra_params: Record<string, unknown>;
  /** v0.9.6 · backend `/health` 深度指标缓存 (gpu_info / cache / model_version). */
  health_meta?: BackendHealthMeta | null;
  error_message: string | null;
  last_checked_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProjectMLBackendsGroup {
  project_id: string;
  project_name: string;
  backends: MLBackendItem[];
}

export interface MLIntegrationsOverview {
  storage: StorageOverview;
  projects: ProjectMLBackendsGroup[];
  total_backends: number;
  connected_backends: number;
}

// ── v0.9.6 · /probe + /runtime-hints ─────────────────────────────────

export interface ProbeRequest {
  url: string;
  auth_method?: "none" | "token";
  auth_token?: string | null;
}

export interface ProbeResponse {
  ok: boolean;
  latency_ms: number;
  status_code?: number | null;
  error?: string | null;
  gpu_info?: Record<string, unknown> | null;
  cache?: Record<string, unknown> | null;
  model_version?: string | null;
}

export interface RuntimeHints {
  ml_backend_default_url: string | null;
  managed_backend_urls?: Record<string, string>;
}

// v0.9.7 · /all 全局 backend 列表 (Wizard step 4 复用 backend 配置).
export interface GlobalBackendItem {
  id: string;
  name: string;
  url: string;
  state: string;
  is_interactive: boolean;
  auth_method: string;
  extra_params: Record<string, unknown>;
  gpu_resource_id: string | null;
  vram_budget_mb: number | null;
  eviction_priority: number | null;
  gpu_config: GPUBackendConfigStatus | null;
  health_meta: BackendHealthMeta | null;
  source_project_id: string;
  source_project_name: string;
  last_checked_at: string | null;
}

export interface GlobalBackendListResponse {
  items: GlobalBackendItem[];
}

// ── v0.19.0 · ADR-0044 · superadmin 全局注册表 CRUD ──────────────────
// 全局 backend (project_id 为 null), 走 /admin/ml-integrations/registry。
export interface MLBackendOut {
  id: string;
  project_id: string | null;
  name: string;
  url: string;
  state: string;
  is_interactive: boolean;
  auth_method: string;
  extra_params: Record<string, unknown>;
  gpu_resource_id: string | null;
  vram_budget_mb: number | null;
  eviction_priority: number;
  health_meta?: BackendHealthMeta | null;
  error_message: string | null;
  last_checked_at: string | null;
  created_at: string;
  updated_at: string;
}

export type MLBackendAuthMethod =
  | "none"
  | "token"
  | `managed:${"gsam2" | "sam3" | "yolo" | "onnxtools" | "rapidocr"}`;

export interface MLBackendRegistryCreatePayload {
  name: string;
  url: string;
  is_interactive?: boolean;
  auth_method?: MLBackendAuthMethod;
  auth_token?: string | null;
  extra_params?: Record<string, unknown>;
  gpu_resource_id?: string | null;
  vram_budget_mb?: number | null;
  eviction_priority?: number;
}

// update 与 create 同字段, 但全部可选 (仅下发改动字段)。
export type MLBackendRegistryUpdatePayload = Partial<MLBackendRegistryCreatePayload>;

export interface RegistryHealthResponse {
  status: "ok" | "error";
  backend_id: string;
  backend_name: string;
}

// ── v0.10.26 · 容器直连观测 (与项目注册解耦) ─────────────────────────
export interface VariantCatalog {
  sam_variant: string[];
  dino_variant: string[];
}

export interface ObserveTarget {
  url: string;
  ok: boolean;
  latency_ms: number;
  status_code?: number | null;
  error?: string | null;
  gpu_info?: GpuInfo | null;
  model_version?: string | null;
  pool?: BackendPoolMeta | null;
  /** v0.10.36 · video tracker 独立显存池观测。 */
  video_pool?: VideoPoolMeta | null;
  /** v0.10.36 · 支持的视频 tracker 列表 (如 ["sam2_video"]); 空 = 不支持视频追踪。 */
  supported_trackers?: string[];
  cache?: { hit_rate?: number; buckets?: Record<string, CacheBucketStat> } | null;
  compute?: MLBackendCompute | null;
  residency?: ResidencyInfo | Record<string, unknown> | null;
  variant_catalog?: VariantCatalog | null;
  supported_variants?: MLBackendSupportedVariantGroup[];
  supports_variants: boolean;
  registered: boolean;
  registered_label?: string | null;
}

export interface ObserveResponse {
  targets: ObserveTarget[];
  configured_count: number;
}

export interface SmokeTestRequest {
  url: string;
  sam_variant?: string;
  dino_variant?: string;
  variant?: Record<string, string>;
}

export interface SmokeTestResponse {
  ok: boolean;
  skipped: boolean;
  reloaded?: boolean | null;
  auto_unloaded: boolean;
  load_latency_ms?: number | null;
  loaded_variant?: { sam_variant?: string; dino_variant?: string } | null;
  message: string;
  error?: string | null;
}

export const adminMlIntegrationsApi = {
  overview: () => apiClient.get<MLIntegrationsOverview>("/admin/ml-integrations/overview"),
  probe: (payload: ProbeRequest) =>
    apiClient.post<ProbeResponse>("/admin/ml-integrations/probe", payload),
  runtimeHints: () => apiClient.get<RuntimeHints>("/admin/ml-integrations/runtime-hints"),
  /** v0.9.7 · 全局 backend 去重列表, 用于 Wizard step 4 dropdown. */
  listAll: () => apiClient.get<GlobalBackendListResponse>("/admin/ml-integrations/all"),
  /** ADR-0049 · 逐物理卡静态容量、模式和配置诊断。 */
  gpuResources: () =>
    apiClient.get<GPUArbiterResourcesResponse>("/admin/ml-integrations/gpu-resources"),
  /** v0.10.26 · 直连观测 env 配的后端容器 (健康/变体目录/registered 标记). */
  observe: () => apiClient.get<ObserveResponse>("/admin/ml-integrations/observe"),
  /** v0.10.26 · 试启动: 空池时 warm→自动 unload 验证可加载性. */
  observeSmokeTest: (payload: SmokeTestRequest) =>
    apiClient.post<SmokeTestResponse>("/admin/ml-integrations/observe/smoke-test", payload),
  /** v0.19.0 · ADR-0044 · superadmin 注册全局 backend (project_id=null); url 重复返 409. */
  createRegistry: (payload: MLBackendRegistryCreatePayload) =>
    apiClient.post<MLBackendOut>("/admin/ml-integrations/registry", payload),
  /** v0.19.0 · 编辑全局 backend; 仅下发改动字段; 不存在返 404. */
  updateRegistry: (id: string, payload: MLBackendRegistryUpdatePayload) =>
    apiClient.put<MLBackendOut>(`/admin/ml-integrations/registry/${id}`, payload),
  /** v0.19.0 · 删除全局 backend; 有运行中预标任务返 409; 不存在返 404. */
  deleteRegistry: (id: string) => apiClient.delete<void>(`/admin/ml-integrations/registry/${id}`),
  /** v0.19.0 · 对全局 backend 触发一次健康检查. */
  registryHealth: (id: string) =>
    apiClient.post<RegistryHealthResponse>(`/admin/ml-integrations/registry/${id}/health`),
  /** ADR-0049 · 不依赖项目绑定的全局 backend 手工卸载。 */
  registryUnload: (id: string) =>
    apiClient.post<MLBackendUnloadResponse>(`/admin/ml-integrations/registry/${id}/unload`),

  // ── v0.23.4 · ADR-0050 · 服务池 / 实例读模型 + CRUD ────────────────────
  // Plan §9.1: 前端不再 URL-join; topology 是注册管理默认读模型。
  /** 角色裁剪的服务池/实例拓扑 (PROJECT_ADMIN 看裁剪视图, SUPER_ADMIN 看全量). */
  topology: () => apiClient.get<TopologyResponse>("/admin/ml-integrations/topology"),
  /** 运行时快照 (SUPER_ADMIN-only); 含 router metrics / health / 新鲜度信封. */
  runtimeSnapshot: () =>
    apiClient.get<RuntimeSnapshotResponse>("/admin/ml-integrations/runtime-snapshot"),
  /** 超管: 全量服务池列表 (含 members + created_at/updated_at). */
  listServicePools: () =>
    apiClient.get<ServicePoolAdminItem[]>("/admin/ml-integrations/service-pools"),
  /** 超管: 新建服务池; name 必填, legacy_instance_id 可选 (单实例起步). */
  createServicePool: (payload: ServicePoolCreateRequest) =>
    apiClient.post<ServicePoolAdminItem>("/admin/ml-integrations/service-pools", payload),
  /** 超管: 单个服务池详情. */
  getServicePool: (poolId: string) =>
    apiClient.get<ServicePoolAdminItem>(`/admin/ml-integrations/service-pools/${poolId}`),
  /** 超管: 改池名 / 启用状态; 仅下发改动字段. */
  patchServicePool: (poolId: string, payload: ServicePoolPatchRequest) =>
    apiClient.patch<ServicePoolAdminItem>(
      `/admin/ml-integrations/service-pools/${poolId}`,
      payload,
    ),
  /** 超管: 删除服务池; 需先解除成员关系且无活跃 lease. */
  deleteServicePool: (poolId: string) =>
    apiClient.delete<void>(`/admin/ml-integrations/service-pools/${poolId}`),
  /** 超管: 加入或更新成员 (weight); capability 指纹不匹配返 409. */
  addOrUpdatePoolMember: (
    poolId: string,
    registryId: string,
    payload: ServicePoolMemberPutRequest,
  ) =>
    apiClient.put<ServicePoolAdminItem>(
      `/admin/ml-integrations/service-pools/${poolId}/members/${registryId}`,
      payload,
    ),
  /** 超管: 移除成员; 需先 drain 且 inflight=0. */
  removePoolMember: (poolId: string, registryId: string) =>
    apiClient.delete<ServicePoolAdminItem>(
      `/admin/ml-integrations/service-pools/${poolId}/members/${registryId}`,
    ),
  /** 超管: 成员 active→draining (停止接新 lease); 幂等. */
  drainPoolMember: (poolId: string, registryId: string) =>
    apiClient.post<ServicePoolAdminItem>(
      `/admin/ml-integrations/service-pools/${poolId}/members/${registryId}/drain`,
    ),
  /** 超管: 成员 draining→active (恢复接流); 幂等; disabled 需先 enable. */
  resumePoolMember: (poolId: string, registryId: string) =>
    apiClient.post<ServicePoolAdminItem>(
      `/admin/ml-integrations/service-pools/${poolId}/members/${registryId}/resume`,
    ),
  /** 超管: 读取已持久化的池基线与成员当前能力差异，不触发写操作. */
  previewCapabilityDrift: (poolId: string, registryId: string) =>
    apiClient.get<CapabilityDriftPreviewResponse>(
      `/admin/ml-integrations/service-pools/${poolId}/members/${registryId}/capability-drift`,
    ),
  /** 超管: 重新探活并接受审核过的能力指纹，原子恢复成员与可选的服务池接流. */
  acceptCapabilityDrift: (
    poolId: string,
    registryId: string,
    payload: CapabilityDriftAcceptRequest,
  ) =>
    apiClient.post<ServicePoolAdminItem>(
      `/admin/ml-integrations/service-pools/${poolId}/members/${registryId}/capability-drift/accept`,
      payload,
    ),
};
