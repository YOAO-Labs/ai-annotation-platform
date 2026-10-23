import { apiClient } from "./client";
import type { StorageBindingOut, StorageProbeOut } from "./generated/types.gen";

export interface StorageHealthResponse {
  status: string;
  bucket: string;
}

export interface BucketSummary {
  name: string;
  status: "ok" | "error";
  object_count: number;
  total_size_bytes: number;
  error?: string;
  role: "annotations" | "datasets" | "bug-reports" | "media-cache" | "audit-archive" | string;
}

export interface BucketsResponse {
  items: BucketSummary[];
  total_object_count: number;
  total_size_bytes: number;
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
  status: "queued";
  asset_type: VideoAssetKind;
  dataset_item_id: string;
}

export const storageApi = {
  bindings: (signal?: AbortSignal) =>
    apiClient.get<StorageBindingOut[]>("/storage-locations/bindings", { signal }),
  probes: (id: string, signal?: AbortSignal) =>
    apiClient.get<StorageProbeOut[]>(`/storage-locations/${id}/probes`, { signal }),
  health: () => apiClient.get<StorageHealthResponse>("/storage/health"),
  buckets: () => apiClient.get<BucketsResponse>("/storage/buckets"),
  videoAssetFailures: (limit = 50, offset = 0, signal?: AbortSignal) =>
    apiClient.get<VideoAssetFailuresResponse>(
      `/storage/video-assets/failures?limit=${limit}&offset=${offset}`,
      { signal },
    ),
  retryVideoAsset: (payload: VideoAssetRetryPayload) =>
    apiClient.post<VideoAssetRetryResponse>("/storage/video-assets/retry", payload),
};
