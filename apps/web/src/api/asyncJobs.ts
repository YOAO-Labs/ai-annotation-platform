// v0.10.16 · async_jobs API client (ROADMAP §1.7)

import { apiClient } from "./client";

export type AsyncJobStatus = "pending" | "running" | "completed" | "failed" | "cancelled";

export type AsyncJobKind =
  | "batch_predict"
  | "video_tracker"
  | "video_correction"
  | "audit_archive"
  | "predictions_import"
  | "prediction_retry"
  | "dataset_scan"
  | "dataset_registration"
  | "dataset_import"
  | "create_tasks"
  | "point_cloud_cross_frame"
  | string;

/** 仅展示 worker 实际支持协作取消的作业。 */
export const CANCELLABLE_ASYNC_JOB_KINDS = new Set<AsyncJobKind>([
  "export",
  "training_dataset",
  "batch_predict",
  "dataset_import",
  "dataset_scan",
  "dataset_registration",
  "storage_usage",
  "managed_original_verification",
  "storage_migration_inventory",
  "storage_migration_copy",
  "dataset_quality",
  "dataset_quality_review",
  "mask_qc",
  "mask_repair",
  "mask_format_import",
  "point_cloud_cross_frame",
]);

export interface StorageJobProgressObservation {
  schema: 1;
  execution_id: string;
  stage:
    | "scanning"
    | "validating"
    | "preflight"
    | "registering"
    | "enumerating"
    | "preparing"
    | "copying"
    | "verifying"
    | "publishing";
  unit: "directories" | "pages" | "entries" | "records" | "objects";
  completed: number;
  sequence: number;
  observed_at: string;
  idle_seconds: number | null;
  attention_after_seconds: 900;
}

export interface AsyncJob {
  id: string;
  kind: AsyncJobKind;
  project_id: string | null;
  user_id: string | null;
  project_display_id: string | null;
  project_name: string | null;
  status: AsyncJobStatus;
  progress_pct: number;
  last_progress_at?: string | null;
  progress_observation?: StorageJobProgressObservation | null;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
  error_message: string | null;
  celery_task_id: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export type AsyncJobOut = AsyncJob;

export interface AsyncJobListResponse {
  items: AsyncJob[];
  total: number;
}

export interface AsyncJobRetryFailedResponse {
  status: "queued";
  job_id: string;
  queued: number;
  skipped: number;
}

export interface AsyncJobListParams {
  kind?: string | string[];
  status?: AsyncJobStatus | AsyncJobStatus[];
  project_id?: string;
  search?: string;
  limit?: number;
  offset?: number;
  /** React Query cancellation for filter/page changes. Not serialized. */
  signal?: AbortSignal;
}

export interface CrossFrameJobCreate {
  operation: "propagate";
  scope: "selected" | "all";
  annotation_ids: string[];
  direction: "forward" | "backward";
  start_frame: number;
  end_frame: number;
  conflict_policy: "skip_existing";
}

export const asyncJobsApi = {
  list: (params: AsyncJobListParams = {}, init?: RequestInit) => {
    const { signal: paramsSignal, ...queryParams } = params;
    const signal = init?.signal ?? paramsSignal;
    const q = new URLSearchParams();
    const statuses = Array.isArray(queryParams.status)
      ? queryParams.status
      : queryParams.status
        ? [queryParams.status]
        : [];
    statuses.forEach((status) => q.append("status", status));
    const kinds = Array.isArray(queryParams.kind)
      ? queryParams.kind
      : queryParams.kind
        ? [queryParams.kind]
        : [];
    kinds.forEach((kind) => q.append("kind", kind));
    if (queryParams.project_id) q.set("project_id", queryParams.project_id);
    if (queryParams.search) q.set("search", queryParams.search);
    if (queryParams.limit !== undefined) q.set("limit", String(queryParams.limit));
    if (queryParams.offset !== undefined) q.set("offset", String(queryParams.offset));
    const qs = q.toString();
    const path = `/async-jobs${qs ? `?${qs}` : ""}`;
    if (init) {
      return apiClient.get<AsyncJobListResponse>(
        path,
        init.signal === signal ? init : { ...init, signal },
      );
    }
    return signal
      ? apiClient.get<AsyncJobListResponse>(path, { signal })
      : apiClient.get<AsyncJobListResponse>(path);
  },
  get: (id: string) => apiClient.get<AsyncJob>(`/async-jobs/${id}`),
  cancel: (id: string) =>
    apiClient.post<{ status: string; id: string }>(`/async-jobs/${id}/cancel`),
  retryFailed: (id: string) =>
    apiClient.post<AsyncJobRetryFailedResponse>(`/async-jobs/${id}/retry-failed`),
  listCrossFrame: (taskId: string, limit = 20, init?: RequestInit) =>
    apiClient.get<AsyncJobListResponse>(
      `/tasks/${taskId}/cross-frame-jobs?limit=${encodeURIComponent(String(limit))}`,
      init,
    ),
  createCrossFrame: (taskId: string, body: CrossFrameJobCreate) =>
    apiClient.post<AsyncJob>(`/tasks/${taskId}/cross-frame-jobs`, body),
  retryCrossFrame: (taskId: string, jobId: string) =>
    apiClient.post<AsyncJob>(`/tasks/${taskId}/cross-frame-jobs/${jobId}/retry-failed`),
};
