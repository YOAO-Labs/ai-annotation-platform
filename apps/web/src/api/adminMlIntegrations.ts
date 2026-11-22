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

export interface CacheBucketStat {
  size?: number;
  hits?: number;
  misses?: number;
  hit_rate?: number;
}

export interface PoolLoadedKey {
  key: string;
  loaded_at?: string;
  last_used_at?: string;
  hit_count?: number;
}

export interface PoolEvictRecord {
  key: string;
  at: string;
  reason: string;
}

export interface BackendPoolMeta {
  cap?: number;
  current_size?: number;
  loaded_keys?: PoolLoadedKey[];
  last_evict?: PoolEvictRecord | null;
  loaded_variants?: Array<{ sam_variant: string; dino_variant: string }>;
  evict_count?: number;
  per_variant_lru_ts?: Record<string, number>;
}

export interface VideoPoolMeta {
  cap: number;
  current_size?: number;
  loaded_keys?: PoolLoadedKey[];
  last_evict?: PoolEvictRecord | null;
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
  pool?: BackendPoolMeta | null;
  video_pool?: VideoPoolMeta | null;
  capabilities?: BackendCapabilities | null;
  compute?: MLBackendCompute | null;
  residency?: ResidencyInfo | Record<string, unknown> | null;
}

export interface BackendCapabilities {
  is_interactive: boolean;
  supported_prompts: string[];
  supported_trackers: string[];
  supported_text_outputs: string[];
  supported_geometric_outputs: string[];
  modalities: string[];
  models?: Array<{ id: string; task?: string }>;
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

export type MLBackendRegistryUpdatePayload = Partial<MLBackendRegistryCreatePayload>;

export interface RegistryHealthResponse {
  status: "ok" | "error";
  backend_id: string;
  backend_name: string;
}

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
  video_pool?: VideoPoolMeta | null;
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
  listAll: () => apiClient.get<GlobalBackendListResponse>("/admin/ml-integrations/all"),
  gpuResources: () =>
    apiClient.get<GPUArbiterResourcesResponse>("/admin/ml-integrations/gpu-resources"),
  observe: () => apiClient.get<ObserveResponse>("/admin/ml-integrations/observe"),
  observeSmokeTest: (payload: SmokeTestRequest) =>
    apiClient.post<SmokeTestResponse>("/admin/ml-integrations/observe/smoke-test", payload),
  createRegistry: (payload: MLBackendRegistryCreatePayload) =>
    apiClient.post<MLBackendOut>("/admin/ml-integrations/registry", payload),
  updateRegistry: (id: string, payload: MLBackendRegistryUpdatePayload) =>
    apiClient.put<MLBackendOut>(`/admin/ml-integrations/registry/${id}`, payload),
  deleteRegistry: (id: string) => apiClient.delete<void>(`/admin/ml-integrations/registry/${id}`),
  registryHealth: (id: string) =>
    apiClient.post<RegistryHealthResponse>(`/admin/ml-integrations/registry/${id}/health`),
  registryUnload: (id: string) =>
    apiClient.post<MLBackendUnloadResponse>(`/admin/ml-integrations/registry/${id}/unload`),

  topology: () => apiClient.get<TopologyResponse>("/admin/ml-integrations/topology"),
  primaryPoolProjectCounts: () =>
    apiClient.get<{ counts: Partial<Record<string, number>> }>(
      "/admin/ml-integrations/primary-pool-project-counts",
    ),
  runtimeSnapshot: () =>
    apiClient.get<RuntimeSnapshotResponse>("/admin/ml-integrations/runtime-snapshot"),
  listServicePools: () =>
    apiClient.get<ServicePoolAdminItem[]>("/admin/ml-integrations/service-pools"),
  createServicePool: (payload: ServicePoolCreateRequest) =>
    apiClient.post<ServicePoolAdminItem>("/admin/ml-integrations/service-pools", payload),
  getServicePool: (poolId: string) =>
    apiClient.get<ServicePoolAdminItem>(`/admin/ml-integrations/service-pools/${poolId}`),
  patchServicePool: (poolId: string, payload: ServicePoolPatchRequest) =>
    apiClient.patch<ServicePoolAdminItem>(
      `/admin/ml-integrations/service-pools/${poolId}`,
      payload,
    ),
  deleteServicePool: (poolId: string) =>
    apiClient.delete<void>(`/admin/ml-integrations/service-pools/${poolId}`),
  addOrUpdatePoolMember: (
    poolId: string,
    registryId: string,
    payload: ServicePoolMemberPutRequest,
  ) =>
    apiClient.put<ServicePoolAdminItem>(
      `/admin/ml-integrations/service-pools/${poolId}/members/${registryId}`,
      payload,
    ),
  removePoolMember: (poolId: string, registryId: string) =>
    apiClient.delete<ServicePoolAdminItem>(
      `/admin/ml-integrations/service-pools/${poolId}/members/${registryId}`,
    ),
  drainPoolMember: (poolId: string, registryId: string) =>
    apiClient.post<ServicePoolAdminItem>(
      `/admin/ml-integrations/service-pools/${poolId}/members/${registryId}/drain`,
    ),
  resumePoolMember: (poolId: string, registryId: string) =>
    apiClient.post<ServicePoolAdminItem>(
      `/admin/ml-integrations/service-pools/${poolId}/members/${registryId}/resume`,
    ),
  previewCapabilityDrift: (poolId: string, registryId: string) =>
    apiClient.get<CapabilityDriftPreviewResponse>(
      `/admin/ml-integrations/service-pools/${poolId}/members/${registryId}/capability-drift`,
    ),
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
