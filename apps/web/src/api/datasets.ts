import { apiClient } from "./client";
import type {
  DatasetOut,
  DatasetLifecyclePreview,
  DatasetLifecycleResult,
  DatasetSavedViewOut,
  DatasetSavedViewWrite,
  DatasetSceneOptions,
  AgentSourceStatus,
} from "./generated/types.gen";
import type { LidarAxisConvention } from "@/types";

function datasetGet<T>(path: string, init?: RequestInit): Promise<T> {
  return init ? apiClient.get<T>(path, init) : apiClient.get<T>(path);
}

export type DatasetResponse = DatasetOut;
export type DatasetLifecycleState = "active" | "archived" | "all";
export type { DatasetLifecyclePreview, DatasetLifecycleResult };

export type DatasetAvailabilityState =
  | "verified"
  | "missing"
  | "changed"
  | "preview_only"
  | "unavailable"
  | "unverified";
export interface DatasetItemAvailability {
  agent_status?: AgentSourceStatus | null;
  state: DatasetAvailabilityState;
  reason_code: string;
  observed_at: string | null;
  preview_available: boolean;
  original_cache_recorded?: boolean;
  original_cache_recorded_at?: string | null;
  original_cache_expires_at?: string | null;
  source_id: string | null;
  source_name: string | null;
  location_id: string | null;
  allowed_actions: string[];
}
export interface DatasetWorkbenchProject {
  id: string;
  name: string;
  display_id: string;
  type_label: string;
  total_tasks: number;
  labeled_tasks: number;
  completed_tasks: number;
  review_tasks: number;
  can_manage: boolean;
}
export interface DatasetWorkbench {
  dataset_id: string;
  availability_counts: Record<string, number>;
  sources: {
    id: string;
    name: string;
    location_id: string;
    enabled: boolean;
    unregistered_at: string | null;
  }[];
  projects: DatasetWorkbenchProject[];
}
export interface DatasetTrainingVersions {
  items: {
    id: string;
    project_id: string;
    project_name: string;
    name: string;
    status: string;
    created_at: string;
  }[];
  total: number;
  limit: number;
  offset: number;
}
export type DatasetSavedView = DatasetSavedViewOut;
export type DatasetSavedViewPayload = DatasetSavedViewWrite;
export interface DatasetItemFilters {
  search?: string;
  source_id?: string;
  scene_id?: string;
  availability?: DatasetAvailabilityState;
  media_type?: "image" | "video" | "point_cloud" | "other";
  extension?: string;
  dimension_state?: "known" | "unknown";
  min_width?: number;
  max_width?: number;
  min_height?: number;
  max_height?: number;
}
export interface DatasetItemListParams extends DatasetItemFilters {
  limit?: number;
  offset?: number;
}
export interface DatasetItemResponse {
  id: string;
  dataset_id: string;
  file_name: string;
  file_path: string;
  file_type: string;
  file_size: number | null;
  content_hash: string | null;
  width: number | null;
  height: number | null;
  metadata: Record<string, unknown>;
  file_url: string | null;
  original_identity_path?: string | null;
  thumbnail_url: string | null;
  blurhash: string | null;
  created_at: string;
  availability?: DatasetItemAvailability;
}

export interface DatasetListResponse {
  items: DatasetResponse[];
  total: number;
  limit: number;
  offset: number;
}

export interface DatasetItemListResponse {
  items: DatasetItemResponse[];
  total: number;
  limit: number;
  offset: number;
}

export interface DatasetCreatePayload {
  name: string;
  description?: string;
  data_type?: string;
  is_temporal?: boolean;
  axis_convention?: LidarAxisConvention | null;
}

export interface DatasetUpdatePayload {
  name?: string;
  description?: string;
  axis_convention?: LidarAxisConvention | null;
}

export interface SniffAxisConventionCandidate {
  convention: LidarAxisConvention;
  score: number;
}

export interface SniffAxisConventionCamera {
  camera_role: string | null;
  best: LidarAxisConvention;
  score: number;
}

export interface SniffAxisConventionResponse {
  best: LidarAxisConvention | null;
  score: number | null;
  candidates: SniffAxisConventionCandidate[];
  source: "task_link" | "dataset_item" | null;
  camera_role: string | null;
  camera_item_id: string | null;
  per_camera: SniffAxisConventionCamera[] | null;
  agreement: number | null;
}

export const datasetsApi = {
  list: (
    params?: {
      search?: string;
      state?: DatasetLifecycleState;
      data_type?: string;
      has_scenes?: boolean;
      location_id?: string;
      limit?: number;
      offset?: number;
    },
    init?: RequestInit,
  ) => {
    const q = new URLSearchParams();
    if (params?.search) q.set("search", params.search);
    if (params?.state) q.set("state", params.state);
    if (params?.data_type) q.set("data_type", params.data_type);
    if (params?.location_id) q.set("location_id", params.location_id);
    if (params?.has_scenes !== undefined) {
      q.set("has_scenes", String(params.has_scenes));
    }
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    const qs = q.toString();
    const path = `/datasets${qs ? `?${qs}` : ""}`;
    return datasetGet<DatasetListResponse>(path, init);
  },

  get: (id: string, init?: RequestInit) => datasetGet<DatasetResponse>(`/datasets/${id}`, init),
  workbench: (id: string, init?: RequestInit) =>
    datasetGet<DatasetWorkbench>(`/datasets/${id}/workbench`, init),
  sceneOptions: (
    id: string,
    params: { limit: number; offset: number; search?: string; selected_id?: string },
    init?: RequestInit,
  ) => {
    const query = new URLSearchParams({
      limit: String(params.limit),
      offset: String(params.offset),
    });
    if (params.search) query.set("search", params.search);
    if (params.selected_id) query.set("selected_id", params.selected_id);
    return datasetGet<DatasetSceneOptions>(`/datasets/${id}/scene-options?${query}`, init);
  },
  item: (id: string, itemId: string, init?: RequestInit) =>
    datasetGet<DatasetItemResponse>(`/datasets/${id}/items/${itemId}`, init),
  trainingVersions: (id: string, params: { limit: number; offset: number }, init?: RequestInit) =>
    datasetGet<DatasetTrainingVersions>(
      `/datasets/${id}/training-versions?${new URLSearchParams({ limit: String(params.limit), offset: String(params.offset) })}`,
      init,
    ),

  previewLifecycle: (
    id: string,
    payload: { operation: "archive" | "restore"; expected_revision: number },
  ) => apiClient.post<DatasetLifecyclePreview>(`/datasets/${id}/lifecycle-preview`, payload),
  setLifecycle: (
    id: string,
    payload: { operation: "archive" | "restore"; expected_revision: number; impact_token: string },
  ) => apiClient.post<DatasetLifecycleResult>(`/datasets/${id}/lifecycle`, payload),

  create: (payload: DatasetCreatePayload) => apiClient.post<DatasetResponse>("/datasets", payload),

  update: (id: string, payload: DatasetUpdatePayload) =>
    apiClient.put<DatasetResponse>(`/datasets/${id}`, payload),

  sniffAxisConvention: (id: string) =>
    apiClient.post<SniffAxisConventionResponse>(`/datasets/${id}/sniff-axis-convention`),

  delete: (id: string) => apiClient.delete<void>(`/datasets/${id}`),

  savedViews: (id: string, init?: RequestInit) =>
    datasetGet<{ items: DatasetSavedView[] }>(`/datasets/${id}/saved-views`, init),
  saveView: (id: string, payload: DatasetSavedViewPayload, viewId?: string) =>
    viewId
      ? apiClient.put<DatasetSavedView>(`/datasets/${id}/saved-views/${viewId}`, payload)
      : apiClient.post<DatasetSavedView>(`/datasets/${id}/saved-views`, payload),
  deleteView: (id: string, viewId: string) =>
    apiClient.delete<void>(`/datasets/${id}/saved-views/${viewId}`),

  listItems: (id: string, params?: DatasetItemListParams, init?: RequestInit) => {
    const q = new URLSearchParams();
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    if (params?.search) q.set("search", params.search);
    if (params?.source_id) q.set("source_id", params.source_id);
    if (params?.scene_id) q.set("scene_id", params.scene_id);
    if (params?.availability) q.set("availability", params.availability);
    (
      [
        "media_type",
        "extension",
        "dimension_state",
        "min_width",
        "max_width",
        "min_height",
        "max_height",
      ] as const
    ).forEach((key) => {
      const value = params?.[key];
      if (value !== undefined) q.set(key, String(value));
    });
    const qs = q.toString();
    return datasetGet<DatasetItemListResponse>(`/datasets/${id}/items${qs ? `?${qs}` : ""}`, init);
  },

  uploadInit: (id: string, payload: { file_name: string; content_type?: string }) =>
    apiClient.post<{ item_id: string; upload_url: string; expires_in: number }>(
      `/datasets/${id}/items/upload-init`,
      payload,
    ),

  uploadComplete: (id: string, itemId: string) =>
    apiClient.post<{ status: string; item_id: string; linked_tasks: number }>(
      `/datasets/${id}/items/upload-complete/${itemId}`,
    ),

  /**
   * 上传 ZIP 包到后端，由服务端解压并入库；最大 200MB。
   * 返回 { added, skipped, errors, total_in_zip, linked_tasks }。
   */
  uploadZip: (
    id: string,
    file: File,
    onProgress?: (pct: number) => void,
  ): Promise<{
    added: number;
    skipped: number;
    errors: Array<{ name: string; error: string }>;
    total_in_zip: number;
    linked_tasks: number;
  }> => {
    return new Promise((resolve, reject) => {
      const token = localStorage.getItem("token");
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/api/v1/datasets/${id}/items/upload-zip`);
      if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress((e.loaded / e.total) * 100);
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(xhr.responseText));
          } catch {
            reject(new Error("响应解析失败"));
          }
        } else {
          let detail = `上传失败 (HTTP ${xhr.status})`;
          try {
            const body = JSON.parse(xhr.responseText);
            if (body.detail) detail = body.detail;
          } catch {
            // ignore
          }
          reject(new Error(detail));
        }
      };
      xhr.onerror = () => reject(new Error("网络错误"));
      const fd = new FormData();
      fd.append("file", file);
      xhr.send(fd);
    });
  },

  scanItems: (id: string) =>
    apiClient.post<{ status: string; new_items: number; linked_tasks: number }>(
      `/datasets/${id}/items/scan`,
    ),

  backfillDimensions: (id: string, batch = 50) =>
    apiClient.post<{ processed: number; failed: number; remaining_hint: boolean }>(
      `/datasets/${id}/backfill-dimensions?batch=${batch}`,
    ),

  backfillMedia: (id: string) =>
    apiClient.post<{ status: string; dataset_id: string }>(`/datasets/${id}/backfill-media`),

  deleteItem: (id: string, itemId: string) =>
    apiClient.delete<void>(`/datasets/${id}/items/${itemId}`),

  linkProject: (id: string, projectId: string) =>
    apiClient.post<{
      status: string;
      dataset_id: string;
      project_id: string;
      // v0.12.0 · 大 dataset(>2000 items) 异步建 task 时返回 job id；小 dataset 同步建则为 null
      async_job_id?: string | null;
      created_tasks?: number;
    }>(`/datasets/${id}/link`, { project_id: projectId }),

  unlinkProject: (id: string, projectId: string) =>
    apiClient.delete<{
      deleted_tasks: number;
      deleted_annotations: number;
      deleted_batches: number;
      deleted_batch_ids: string[];
      soft: boolean;
    }>(`/datasets/${id}/link/${projectId}`),

  previewUnlink: (id: string, projectId: string, init?: RequestInit) =>
    datasetGet<{
      will_delete_tasks: number;
      will_delete_annotations: number;
      will_delete_batches: number;
    }>(`/datasets/${id}/link/${projectId}/preview-unlink`, init),

  getLinkedProjects: (id: string, init?: RequestInit) =>
    datasetGet<import("./projects").ProjectResponse[]>(`/datasets/${id}/projects`, init),

  // v0.7.3 · 项目侧反查：列出本项目已关联的所有数据集
  listForProject: (projectId: string, init?: RequestInit) =>
    datasetGet<
      {
        id: string;
        display_id: string;
        name: string;
        data_type: string;
        linked_at: string | null;
        items_count: number;
        tasks_in_project: number;
      }[]
    >(`/projects/${projectId}/datasets`, init),
};
