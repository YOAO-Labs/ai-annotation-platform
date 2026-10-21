import { apiClient } from "./client";

export interface PreannotateQueueItem {
  batch_id: string;
  batch_name: string;
  batch_status: string;
  project_id: string;
  project_name: string;
  project_display_id?: string | null;
  total_tasks: number;
  prediction_count: number;
  failed_count: number;
  last_run_at?: string | null;
  can_retry: boolean;
}

export interface PreannotateQueueResponse {
  items: PreannotateQueueItem[];
}

// v0.9.12 · BUG B-16 多选批量清理 + B-17 项目卡片聚合
export type BulkClearMode = "predictions_only" | "reset_to_draft";

export interface BulkClearRequest {
  batch_ids: string[];
  mode: BulkClearMode;
  reason: string;
}

export interface BulkClearItem {
  batch_id: string;
  reason: string;
}

export interface BulkClearResponse {
  succeeded: string[];
  skipped: BulkClearItem[];
  failed: BulkClearItem[];
}

export interface PreannotateProjectSummary {
  project_id: string;
  project_name: string;
  project_display_id?: string | null;
  type_key: string;
  // v0.10.28 · 媒体维度数据类型 (image / video / lidar).
  data_type: string;
  ml_backend_id?: string | null;
  ml_backend_name?: string | null;
  ml_backend_state?: string | null;
  ml_backend_max_concurrency?: number | null;
  ready_batches: number;
  active_batches: number;
  last_job_at?: string | null;
  recent_failures: number;
}

export interface PreannotateProjectSummaryResponse {
  items: PreannotateProjectSummary[];
}

export const adminPreannotateApi = {
  /** v0.9.6 · 列出 pre_annotated 状态批次 + prediction/failed 计数. */
  queue: (
    limit = 50,
    options: { projectId?: string; offset?: number; signal?: AbortSignal } = {},
  ) => {
    const query = new URLSearchParams({ limit: String(limit) });
    if (options.projectId) query.set("project_id", options.projectId);
    if (options.offset) query.set("offset", String(options.offset));
    return apiClient.get<PreannotateQueueResponse>(`/admin/preannotate-queue?${query}`, {
      signal: options.signal,
    });
  },

  /** Read the complete project collection before client-side search, sorting and selection. */
  projectQueue: async (
    projectId: string,
    signal?: AbortSignal,
  ): Promise<PreannotateQueueResponse> => {
    const items: PreannotateQueueItem[] = [];
    const limit = 200;
    for (let offset = 0; ; offset += limit) {
      const next = await adminPreannotateApi.queue(limit, { projectId, offset, signal });
      items.push(...next.items);
      if (next.items.length < limit) break;
    }
    return { items };
  },

  /** v0.9.12 B-16 · 多选批量清理 prediction (predictions_only=回 active / reset_to_draft=全重置). */
  bulkClear: (payload: BulkClearRequest) =>
    apiClient.post<BulkClearResponse>(`/admin/preannotate-queue/bulk-clear`, payload),

  /** v0.9.12 B-17 · 项目卡片聚合 (仅返回有 ml_backend 的项目). */
  summary: () => apiClient.get<PreannotateProjectSummaryResponse>(`/admin/preannotate-summary`),
};
