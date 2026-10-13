import { apiClient } from "./client";
import type { ExportTarget, ExportOptions } from "./projects";
import type { UserBrief } from "@/types";

export interface BatchResponse {
  id: string;
  project_id: string;
  dataset_id: string | null;
  display_id: string;
  name: string;
  description: string;
  status: string;
  priority: number;
  deadline: string | null;
  /** v0.7.2 派生字段（= [annotator_id, reviewer_id] filter null）；保留为兼容旧路径，新代码优先用 annotator/reviewer。 */
  assigned_user_ids: string[];
  /** v0.7.2 · 单值分派 */
  annotator_id: string | null;
  reviewer_id: string | null;
  /** 责任人 brief（avatar / name / role） */
  annotator: UserBrief | null;
  reviewer: UserBrief | null;
  total_tasks: number;
  completed_tasks: number;
  review_tasks: number;
  approved_tasks: number;
  rejected_tasks: number;
  progress_pct: number;
  created_by: string | null;
  created_at: string;
  updated_at: string | null;
  review_feedback: string | null;
  reviewed_at: string | null;
  reviewed_by: string | null;
  // v0.9.15 · ADR-0008 admin-lock
  admin_locked: boolean;
  admin_lock_reason: string | null;
  admin_locked_at: string | null;
  admin_locked_by: string | null;
}

export interface BatchCreatePayload {
  name: string;
  description?: string;
  dataset_id?: string;
  priority?: number;
  deadline?: string;
  annotator_id?: string | null;
  reviewer_id?: string | null;
}

export interface BatchUpdatePayload {
  name?: string;
  description?: string;
  priority?: number;
  deadline?: string;
  annotator_id?: string | null;
  reviewer_id?: string | null;
}

export interface BatchSplitPayload {
  strategy: "metadata" | "id_range" | "random" | "by_scene";
  metadata_key?: string;
  metadata_value?: string;
  item_ids?: string[];
  n_batches?: number;
  /** v0.11.22 · false = 顺序切分（不打乱），按 task 创建顺序分包 */
  shuffle?: boolean;
  name_prefix?: string;
  priority?: number;
  deadline?: string;
  annotator_id?: string | null;
  reviewer_id?: string | null;
}

export interface ProjectDistributeBatchesPayload {
  batch_ids?: string[];
  annotator_ids?: string[];
  reviewer_ids?: string[];
  only_unassigned?: boolean;
  preview_version?: string;
}

export interface BatchDistributeResultResponse {
  distributed_batches: number;
  annotator_per_batch: Record<string, string | null>;
  reviewer_per_batch: Record<string, string | null>;
}

export interface BatchDistributionPreviewItem {
  batch_id: string;
  display_id: string;
  name: string;
  status: string;
  before_annotator_id: string | null;
  after_annotator_id: string | null;
  before_reviewer_id: string | null;
  after_reviewer_id: string | null;
  task_count: number;
  will_change: boolean;
  skipped_reason?: string | null;
}

export interface BatchDistributionPreview {
  project_id: string;
  only_unassigned: boolean;
  total_batches: number;
  candidate_batches: number;
  changed_batches: number;
  skipped_batches: number;
  items: BatchDistributionPreviewItem[];
  recipient_summary?: Array<{
    user_id: string;
    role: string;
    new_task_count: number;
    existing_backlog_count: number;
  }>;
  preview_version: string;
}

// v0.7.3 · 多选批量操作
export interface BulkBatchActionItem {
  batch_id: string;
  reason: string;
}

export interface BulkBatchActionResponse {
  succeeded: string[];
  skipped: BulkBatchActionItem[];
  failed: BulkBatchActionItem[];
}

export interface BulkBatchReassignPayload {
  batch_ids: string[];
  annotator_id?: string | null;
  reviewer_id?: string | null;
}

// v0.7.3 · 批次操作历史抽屉
export interface BatchAuditLogEntry {
  id: number;
  created_at: string | null;
  actor_id: string | null;
  actor_email: string | null;
  actor_role: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  detail: Record<string, unknown> | null;
}

export interface BatchReject {
  feedback: string;
}

export interface BatchSubmitResult {
  batch_id: string;
  status: string;
  submitted_tasks: number;
  skipped_tasks: number;
  remaining_tasks: number;
}

export const batchesApi = {
  list: (projectId: string, status?: string) => {
    const q = new URLSearchParams();
    if (status) q.set("status", status);
    const qs = q.toString();
    return apiClient.get<BatchResponse[]>(`/projects/${projectId}/batches${qs ? `?${qs}` : ""}`);
  },

  get: (projectId: string, batchId: string) =>
    apiClient.get<BatchResponse>(`/projects/${projectId}/batches/${batchId}`),

  create: (projectId: string, payload: BatchCreatePayload) =>
    apiClient.post<BatchResponse>(`/projects/${projectId}/batches`, payload),

  update: (projectId: string, batchId: string, payload: BatchUpdatePayload) =>
    apiClient.patch<BatchResponse>(`/projects/${projectId}/batches/${batchId}`, payload),

  remove: (projectId: string, batchId: string, force = false) =>
    apiClient.delete<void>(
      `/projects/${projectId}/batches/${batchId}${force ? "?force=true" : ""}`,
    ),

  transition: (projectId: string, batchId: string, targetStatus: string, reason?: string) =>
    apiClient.post<BatchResponse>(
      `/projects/${projectId}/batches/${batchId}/transition`,
      reason ? { target_status: targetStatus, reason } : { target_status: targetStatus },
    ),

  // 整批送审：提交批次内所有 pending / in_progress 任务，再推进到 reviewing。
  submitReview: (projectId: string, batchId: string) =>
    apiClient.post<BatchSubmitResult>(
      `/projects/${projectId}/batches/${batchId}/submit-review`,
      {},
    ),

  split: (projectId: string, payload: BatchSplitPayload) =>
    apiClient.post<BatchResponse[]>(`/projects/${projectId}/batches/split`, payload),

  reject: (projectId: string, batchId: string, feedback: string) =>
    apiClient.post<BatchResponse>(`/projects/${projectId}/batches/${batchId}/reject`, { feedback }),

  // v0.7.6 · 终极重置：任意状态 → draft（owner-only，reason ≥ 10 字）
  reset: (projectId: string, batchId: string, reason: string) =>
    apiClient.post<BatchResponse>(`/projects/${projectId}/batches/${batchId}/reset`, { reason }),

  distributeBatches: (projectId: string, payload: ProjectDistributeBatchesPayload) =>
    apiClient.post<BatchDistributeResultResponse>(
      `/projects/${projectId}/batches/distribute-batches`,
      payload,
    ),

  applyDistribution: (projectId: string, payload: ProjectDistributeBatchesPayload) =>
    apiClient.post<BatchDistributeResultResponse>(
      `/projects/${projectId}/batches/distribution-apply`,
      payload,
    ),

  previewAssignment: (
    projectId: string,
    batchId: string,
    payload: { annotator_id: string | null; reviewer_id: string | null },
  ) =>
    apiClient.post<BatchDistributionPreview>(
      `/projects/${projectId}/batches/${batchId}/assignment-preview`,
      payload,
    ),
  applyAssignment: (
    projectId: string,
    batchId: string,
    payload: { annotator_id: string | null; reviewer_id: string | null; preview_version: string },
  ) =>
    apiClient.post<BatchResponse>(
      `/projects/${projectId}/batches/${batchId}/assignment-apply`,
      payload,
    ),

  previewDistribution: (projectId: string, payload: ProjectDistributeBatchesPayload) =>
    apiClient.post<BatchDistributionPreview>(
      `/projects/${projectId}/batches/distribution-preview`,
      payload,
    ),

  // v0.7.3 · 批量操作
  bulkArchive: (projectId: string, batchIds: string[]) =>
    apiClient.post<BulkBatchActionResponse>(`/projects/${projectId}/batches/bulk-archive`, {
      batch_ids: batchIds,
    }),

  bulkDelete: (projectId: string, batchIds: string[], force = false) =>
    apiClient.post<BulkBatchActionResponse>(
      `/projects/${projectId}/batches/bulk-delete${force ? "?force=true" : ""}`,
      { batch_ids: batchIds },
    ),

  bulkReassign: (projectId: string, payload: BulkBatchReassignPayload) =>
    apiClient.post<BulkBatchActionResponse>(
      `/projects/${projectId}/batches/bulk-reassign`,
      payload,
    ),

  bulkActivate: (projectId: string, batchIds: string[]) =>
    apiClient.post<BulkBatchActionResponse>(`/projects/${projectId}/batches/bulk-activate`, {
      batch_ids: batchIds,
    }),

  // v0.9.15 · ADR-0008 admin-lock
  adminLock: (projectId: string, batchId: string, reason: string) =>
    apiClient.post<BatchResponse>(`/projects/${projectId}/batches/${batchId}/admin-lock`, {
      reason,
    }),

  adminUnlock: (projectId: string, batchId: string) =>
    apiClient.post<BatchResponse>(`/projects/${projectId}/batches/${batchId}/admin-unlock`, {}),

  // v0.9.15 · Bulk approve/reject
  bulkApprove: (projectId: string, batchIds: string[]) =>
    apiClient.post<BulkBatchActionResponse>(`/projects/${projectId}/batches/bulk-approve`, {
      batch_ids: batchIds,
    }),

  bulkReject: (projectId: string, batchIds: string[], feedback: string) =>
    apiClient.post<BulkBatchActionResponse>(`/projects/${projectId}/batches/bulk-reject`, {
      batch_ids: batchIds,
      feedback,
    }),

  // v0.7.3 · 批次操作历史
  auditLogs: (projectId: string, batchId: string, limit = 50) =>
    apiClient.get<BatchAuditLogEntry[]>(
      `/projects/${projectId}/batches/${batchId}/audit-logs?limit=${limit}`,
    ),

  // v0.7.3 · 未归类任务计数（batch_id IS NULL）— 顶部横带提示用
  unclassifiedCount: (projectId: string) =>
    apiClient.get<{ count: number }>(`/projects/${projectId}/batches/unclassified-count`),

  // v0.10.27 · 导出异步化：POST 创建 async_job(kind=export)，返回 {job_id}。
  // 不再直接 blob 下载；产物完成后在 JobsBell 里用预签名 URL 下载。
  exportBatch: (
    projectId: string,
    batchId: string,
    targets: ExportTarget[],
    opts?: ExportOptions,
  ) => {
    const includeAttr = opts?.includeAttributes !== false;
    const params = new URLSearchParams({ include_attributes: String(includeAttr) });
    targets.forEach((t) => params.append("targets", t));
    if (opts?.videoFrameMode) params.set("video_frame_mode", opts.videoFrameMode);
    if (opts?.videoOverlapPolicy) {
      params.set("video_overlap_policy", opts.videoOverlapPolicy);
    }
    if (opts?.motsFrameBase !== undefined) {
      params.set("mots_frame_base", String(opts.motsFrameBase));
    }
    return apiClient.post<{ job_id: string }>(
      `/projects/${projectId}/batches/${batchId}/export?${params.toString()}`,
      {
        ...(opts?.scope ? { scope: opts.scope } : {}),
        ...(opts?.lidar ? { lidar: { kitti_camera_role: opts.lidar.kittiCameraRole } } : {}),
      },
    );
  },
};
