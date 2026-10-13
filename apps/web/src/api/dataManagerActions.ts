import type { ExportTarget, VideoFrameMode } from "./projects";
import { apiClient } from "./client";

export const MAX_DATA_MANAGER_TASK_IDS = 200;

export type DataManagerTaskAssignmentPayload = {
  task_ids: string[];
  annotator_id?: string | null;
  reviewer_id?: string | null;
};

export type DataManagerTaskAssignmentApplyPayload = DataManagerTaskAssignmentPayload & {
  preview_version: string;
};

export interface DataManagerTaskAssignmentItem {
  task_id: string;
  task_display_id: string | null;
  batch_id: string | null;
  status: string | null;
  task_updated_at: string | null;
  before_annotator_id: string | null;
  after_annotator_id: string | null;
  before_reviewer_id: string | null;
  after_reviewer_id: string | null;
  effective_before_annotator_id?: string | null;
  effective_after_annotator_id?: string | null;
  effective_before_reviewer_id?: string | null;
  effective_after_reviewer_id?: string | null;
  will_change: boolean;
  reason: string | null;
}

export interface DataManagerTaskAssignmentResponse {
  task_ids: string[];
  preview_version: string;
  eligible_count: number;
  skipped_count: number;
  failed_count: number;
  succeeded: string[];
  items: DataManagerTaskAssignmentItem[];
}

export interface DataManagerTaskExportPayload {
  task_ids: string[];
  targets: ExportTarget[];
  include_attributes?: boolean;
  video_frame_mode?: VideoFrameMode;
  axis_frame?: "iso" | "source";
  indexed_overlap_policy?: "error" | "z_order" | "larger_area" | "smaller_area";
  video_overlap_policy?: "error" | "z_order" | "larger_area" | "smaller_area";
  mots_frame_base?: 0 | 1;
  lidar?: { kitti_camera_role?: string };
}

export type DataManagerTaskPreannotationPayload = Record<string, unknown> & {
  ml_backend_id: string;
  task_ids: string[];
};

export interface DataManagerActionRequestOptions {
  /** Reuse the same key when a network retry must not enqueue a second job. */
  idempotencyKey?: string;
}

export interface DataManagerTaskJobResponse {
  job_id: string;
  status: string;
  celery_task_id?: string | null;
}

function requestOptions(options?: DataManagerActionRequestOptions): RequestInit | undefined {
  return options?.idempotencyKey
    ? { headers: { "Idempotency-Key": options.idempotencyKey } }
    : undefined;
}

function taskIds(taskIds: readonly string[]): string[] {
  const ids = [...new Set(taskIds)];
  if (ids.length === 0) throw new Error("Select at least one task");
  if (ids.length > MAX_DATA_MANAGER_TASK_IDS) {
    throw new Error(`Select at most ${MAX_DATA_MANAGER_TASK_IDS} tasks`);
  }
  return ids;
}

export const dataManagerTaskActionsApi = {
  assignmentPreview: (projectId: string, payload: DataManagerTaskAssignmentPayload) =>
    apiClient.post<DataManagerTaskAssignmentResponse>(
      `/projects/${projectId}/data-manager/tasks/assignment-preview`,
      { ...payload, task_ids: taskIds(payload.task_ids) },
    ),

  assignmentApply: (projectId: string, payload: DataManagerTaskAssignmentApplyPayload) =>
    apiClient.post<DataManagerTaskAssignmentResponse>(
      `/projects/${projectId}/data-manager/tasks/assignment-apply`,
      { ...payload, task_ids: taskIds(payload.task_ids) },
    ),

  exportTasks: (
    projectId: string,
    payload: DataManagerTaskExportPayload,
    options?: DataManagerActionRequestOptions,
  ) =>
    apiClient.post<DataManagerTaskJobResponse>(
      `/projects/${projectId}/data-manager/tasks/export`,
      {
        ...payload,
        task_ids: taskIds(payload.task_ids),
      },
      requestOptions(options),
    ),

  preannotate: (
    projectId: string,
    payload: DataManagerTaskPreannotationPayload,
    options?: DataManagerActionRequestOptions,
  ) =>
    apiClient.post<DataManagerTaskJobResponse>(
      `/projects/${projectId}/preannotate`,
      {
        ...payload,
        task_ids: taskIds(payload.task_ids),
      },
      requestOptions(options),
    ),
};

export { taskIds as canonicalDataManagerTaskIds };
