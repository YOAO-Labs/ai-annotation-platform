import { apiClient } from "./client";
import type { DatasetOut } from "./generated/types.gen";
import type { LidarAxisConvention } from "@/types";

export type DatasetResponse = DatasetOut;

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
  thumbnail_url: string | null;
  blurhash: string | null;
  created_at: string;
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
      data_type?: string;
      has_scenes?: boolean;
      limit?: number;
      offset?: number;
    },
    init?: RequestInit,
  ) => {
    const q = new URLSearchParams();
    if (params?.search) q.set("search", params.search);
    if (params?.data_type) q.set("data_type", params.data_type);
    if (params?.has_scenes !== undefined) {
      q.set("has_scenes", String(params.has_scenes));
    }
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    const qs = q.toString();
    const path = `/datasets${qs ? `?${qs}` : ""}`;
    return init
      ? apiClient.get<DatasetListResponse>(path, init)
      : apiClient.get<DatasetListResponse>(path);
  },

  get: (id: string) => apiClient.get<DatasetResponse>(`/datasets/${id}`),

  create: (payload: DatasetCreatePayload, options?: { idempotencyKey?: string }) =>
    apiClient.post<DatasetResponse>("/datasets", payload, {
      headers: { "Idempotency-Key": options?.idempotencyKey ?? crypto.randomUUID() },
    }),

  update: (id: string, payload: DatasetUpdatePayload, options?: { idempotencyKey?: string }) =>
    apiClient.put<DatasetResponse>(`/datasets/${id}`, payload, {
      headers: { "Idempotency-Key": options?.idempotencyKey ?? crypto.randomUUID() },
    }),

  sniffAxisConvention: (id: string) =>
    apiClient.post<SniffAxisConventionResponse>(`/datasets/${id}/sniff-axis-convention`),

  delete: (id: string, options?: { idempotencyKey?: string }) =>
    apiClient.delete<void>(`/datasets/${id}`, undefined, {
      headers: { "Idempotency-Key": options?.idempotencyKey ?? crypto.randomUUID() },
    }),

  listItems: (id: string, params?: { limit?: number; offset?: number }) => {
    const q = new URLSearchParams();
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    const qs = q.toString();
    return apiClient.get<DatasetItemListResponse>(`/datasets/${id}/items${qs ? `?${qs}` : ""}`);
  },

  uploadInit: (
    id: string,
    payload: { file_name: string; content_type?: string },
    options?: { idempotencyKey?: string },
  ) =>
    apiClient.post<{ item_id: string; upload_url: string; expires_in: number }>(
      `/datasets/${id}/items/upload-init`,
      payload,
      { headers: { "Idempotency-Key": options?.idempotencyKey ?? crypto.randomUUID() } },
    ),

  uploadComplete: (id: string, itemId: string, options?: { idempotencyKey?: string }) =>
    apiClient.post<{ status: string; item_id: string; linked_tasks: number }>(
      `/datasets/${id}/items/upload-complete/${itemId}`,
      undefined,
      { headers: { "Idempotency-Key": options?.idempotencyKey ?? crypto.randomUUID() } },
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

  deleteItem: (id: string, itemId: string, options?: { idempotencyKey?: string }) =>
    apiClient.delete<void>(`/datasets/${id}/items/${itemId}`, undefined, {
      headers: { "Idempotency-Key": options?.idempotencyKey ?? crypto.randomUUID() },
    }),

  linkProject: (id: string, projectId: string) =>
    apiClient.post<{
      status: string;
      dataset_id: string;
      project_id: string;
      // v0.12.0 · 大 dataset(>2000 items) 异步建 task 时返回 job id；小 dataset 同步建则为 null
      async_job_id?: string | null;
      created_tasks?: number;
    }>(`/datasets/${id}/link`, { project_id: projectId }),

  unlinkProject: (id: string, projectId: string, options?: { idempotencyKey?: string }) =>
    apiClient.delete<{
      deleted_tasks: number;
      deleted_annotations: number;
      deleted_batches: number;
      deleted_batch_ids: string[];
      soft: boolean;
    }>(`/datasets/${id}/link/${projectId}`, undefined, {
      headers: { "Idempotency-Key": options?.idempotencyKey ?? crypto.randomUUID() },
    }),

  previewUnlink: (id: string, projectId: string) =>
    apiClient.get<{
      will_delete_tasks: number;
      will_delete_annotations: number;
      will_delete_batches: number;
    }>(`/datasets/${id}/link/${projectId}/preview-unlink`),

  getLinkedProjects: (id: string) =>
    apiClient.get<import("./projects").ProjectResponse[]>(`/datasets/${id}/projects`),

  // v0.7.3 · 项目侧反查：列出本项目已关联的所有数据集
  listForProject: (projectId: string) =>
    apiClient.get<
      {
        id: string;
        display_id: string;
        name: string;
        data_type: string;
        linked_at: string | null;
        items_count: number;
        tasks_in_project: number;
      }[]
    >(`/projects/${projectId}/datasets`),
};
