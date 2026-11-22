import { apiClient } from "./client";

export type VideoTrackerJobStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "pending_review"
  | "partially_reviewed"
  | "accepted"
  | "discarded";

export interface VideoTrackerJobListItem {
  id: string;
  task_id: string | null;
  project_id: string;
  project_name: string | null;
  project_display_id: string | null;
  dataset_item_id: string | null;
  annotation_id: string | null;
  segment_id: string | null;
  created_by: string | null;
  status: VideoTrackerJobStatus;
  model_key: string | null;
  direction: string | null;
  from_frame: number | null;
  to_frame: number | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string | null;
}

export type VideoTrackerJobCounts = Record<VideoTrackerJobStatus, number>;

export interface VideoTrackerJobsResponse {
  items: VideoTrackerJobListItem[];
  next_cursor: string | null;
  counts: VideoTrackerJobCounts;
}

export interface ListVideoTrackerJobsParams {
  project_id?: string;
  status?: VideoTrackerJobStatus;
  model_key?: string;
  cursor?: string;
  limit?: number;
  signal?: AbortSignal;
}

export const videoTrackerJobsApi = {
  list: (params: ListVideoTrackerJobsParams = {}, init?: RequestInit) => {
    const { signal: paramsSignal, ...queryParams } = params;
    const signal = init?.signal ?? paramsSignal;
    const qs = new URLSearchParams();
    if (queryParams.project_id) qs.set("project_id", queryParams.project_id);
    if (queryParams.status) qs.set("status", queryParams.status);
    if (queryParams.model_key) qs.set("model_key", queryParams.model_key);
    if (queryParams.cursor) qs.set("cursor", queryParams.cursor);
    if (queryParams.limit !== undefined) qs.set("limit", String(queryParams.limit));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    const path = `/video-tracker-jobs${suffix}`;
    if (init) {
      return apiClient.get<VideoTrackerJobsResponse>(
        path,
        init.signal === signal ? init : { ...init, signal },
      );
    }
    return signal
      ? apiClient.get<VideoTrackerJobsResponse>(path, { signal })
      : apiClient.get<VideoTrackerJobsResponse>(path);
  },
};
