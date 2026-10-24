import { apiClient } from "./client";
import type {
  StorageBindingOut,
  StorageProbeOut,
  StorageUsageBreakdown,
} from "./generated/types.gen";

export interface StorageHealthResponse {
  status: string;
  bucket: string;
}

export interface BucketSummary {
  name: string;
  role: string;
  roles: string[];
  status: "unknown" | "ok" | "stale";
  object_count: number | null;
  total_size_bytes: number | null;
  error: string | null;
  measured_at: string | null;
  measurement_started_at: string | null;
  attempt_status: string;
  attempted_at: string | null;
  scope: string;
  source: string;
  coverage: "none" | "complete";
  is_estimate: boolean;
}

export interface BucketsResponse {
  items: BucketSummary[];
  total_object_count: number | null;
  total_size_bytes: number | null;
  measured_bucket_count: number;
  bucket_count: number;
  physical_total_bytes: number | null;
  physical_available_bytes: number | null;
  quota_bytes: number | null;
  inventory_job_id: string | null;
  inventory_status: string | null;
  location_id: string;
  breakdown?: StorageUsageBreakdown | null;
}

export type VideoAssetKind = "probe" | "poster" | "frame_timetable" | "chunk" | "frame";

export interface VideoAssetFailureItem {
  asset_key: string;
  asset_type: VideoAssetKind;
  dataset_item_id: string;
  file_name: string;
  task_id: string | null;
  task_display_id: string | null;
  project_id: string | null;
  project_name: string | null;
  error: string;
  updated_at: string | null;
  chunk_id: number | null;
  frame_index: number | null;
  width: number | null;
  format: string | null;
}

export interface VideoAssetFailuresResponse {
  items: VideoAssetFailureItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface VideoAssetRetryPayload {
  asset_type: VideoAssetKind;
  dataset_item_id: string;
  chunk_id?: number | null;
  frame_index?: number | null;
  width?: number | null;
  format?: "webp" | "jpeg" | null;
}

export interface VideoAssetRetryResponse {
  status: "queued" | "dispatch_pending";
  asset_type: VideoAssetKind;
  dataset_item_id: string;
  job_id: string;
}

export const storageApi = {
  bindings: (signal?: AbortSignal) =>
    apiClient.get<StorageBindingOut[]>("/storage-locations/bindings", { signal }),
  probes: (id: string, signal?: AbortSignal) =>
    apiClient.get<StorageProbeOut[]>(`/storage-locations/${id}/probes`, { signal }),
  health: () => apiClient.get<StorageHealthResponse>("/storage/health"),
  buckets: (signal?: AbortSignal) => apiClient.get<BucketsResponse>("/storage/buckets", { signal }),
  startInventory: () =>
    apiClient.post<{ job_id: string; reused: boolean }>("/storage/usage-inventory"),
  videoAssetFailures: (limit = 50, offset = 0, signal?: AbortSignal) =>
    apiClient.get<VideoAssetFailuresResponse>(
      `/storage/video-assets/failures?limit=${limit}&offset=${offset}`,
      { signal },
    ),
  retryVideoAsset: (payload: VideoAssetRetryPayload) =>
    apiClient.post<VideoAssetRetryResponse>("/storage/video-assets/retry", payload),
};
