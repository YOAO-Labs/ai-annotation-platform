import { apiClient } from "./client";
import type {
  StorageBindingOut,
  BugAttachmentBindingPreview,
  AuditArchiveBindingPreview,
  ExportBindingPreview,
  ImportBindingPreview,
  AvatarBindingApply,
  AvatarBindingCandidates,
  AvatarBindingChange,
  AvatarBindingPreview,
  StorageProbeOut,
  StorageUsageBreakdown,
  StorageFaultList,
} from "./generated/types.gen";

export type StorageFaultFilter = "active" | "recovered" | "all";

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
  uncovered_binding_purposes?: string[];
  accounting?: StorageUsageAccountingSummary | null;
  reconciliation?: StorageUsageReconciliationSummary | null;
  usage_schedule?: UsageScheduleSummary | null;
}

export interface StorageUsageAccountingSummary {
  scope: "current_objects";
  coverage: "partial";
  producers: string[];
  confirmed_added_bytes: number;
  confirmed_removed_bytes: number;
  net_change_bytes: number;
  pending_operation_count: number;
  unknown_operation_count: number;
  projected_total_bytes: null;
}

export interface StorageUsageReconciliationSummary {
  scope: "current_objects";
  coverage: "partial";
  status: "no_baseline" | "partial";
  measured_at: string;
  bucket_count: number;
  compared_object_count: number;
  uncertain_object_count: number;
  unexplained_object_count: number;
  confirmed_change_bytes: number;
  known_unexplained_change_bytes: number;
  unexplained_change_bytes: number | null;
}

export interface UsageScheduleSummary {
  enabled: boolean;
  interval_hours: number;
  status: "off" | "active" | "paused";
  next_due_at: string | null;
  last_started_at: string | null;
  pause_reason: string | null;
}

export interface UsageSchedulePayload {
  enabled: boolean;
  interval_hours: number;
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
  faults: (id: string, state: StorageFaultFilter, offset: number, signal?: AbortSignal) =>
    apiClient.get<StorageFaultList>(
      `/storage-locations/${id}/faults?${new URLSearchParams({ state, limit: "20", offset: String(offset) })}`,
      { signal },
    ),
  bindings: (signal?: AbortSignal) =>
    apiClient.get<StorageBindingOut[]>("/storage-locations/bindings", { signal }),
  avatarBindingCandidates: (offset: number, signal?: AbortSignal, limit = 20) =>
    apiClient.get<AvatarBindingCandidates>(
      `/storage-locations/bindings/avatars/candidates?limit=${limit}&offset=${offset}`,
      { signal },
    ),
  previewAvatarBinding: (payload: AvatarBindingChange) =>
    apiClient.post<AvatarBindingPreview>(
      "/storage-locations/bindings/avatars/change-preview",
      payload,
    ),
  applyAvatarBinding: (payload: AvatarBindingApply) =>
    apiClient.patch<StorageBindingOut>("/storage-locations/bindings/avatars", payload),
  bindingCandidates: (
    purpose: "avatars" | "bug-reports" | "audit-archive" | "export" | "import",
    offset: number,
    signal?: AbortSignal,
    limit = 20,
  ) =>
    purpose === "avatars"
      ? storageApi.avatarBindingCandidates(offset, signal, limit)
      : apiClient.get<AvatarBindingCandidates>(
          `/storage-locations/bindings/${purpose}/candidates?limit=${limit}&offset=${offset}`,
          { signal },
        ),
  previewBinding: (
    purpose: "avatars" | "bug-reports" | "audit-archive" | "export" | "import",
    payload: AvatarBindingChange,
  ) =>
    purpose === "avatars"
      ? storageApi.previewAvatarBinding(payload)
      : apiClient.post<
          | BugAttachmentBindingPreview
          | AuditArchiveBindingPreview
          | ExportBindingPreview
          | ImportBindingPreview
        >(`/storage-locations/bindings/${purpose}/change-preview`, payload),
  applyBinding: (
    purpose: "avatars" | "bug-reports" | "audit-archive" | "export" | "import",
    payload: AvatarBindingApply,
  ) =>
    purpose === "avatars"
      ? storageApi.applyAvatarBinding(payload)
      : apiClient.patch<StorageBindingOut>(`/storage-locations/bindings/${purpose}`, payload),
  probes: (id: string, signal?: AbortSignal) =>
    apiClient.get<StorageProbeOut[]>(`/storage-locations/${id}/probes`, { signal }),
  health: () => apiClient.get<StorageHealthResponse>("/storage/health"),
  buckets: (signal?: AbortSignal) => apiClient.get<BucketsResponse>("/storage/buckets", { signal }),
  startInventory: () =>
    apiClient.post<{ job_id: string; reused: boolean }>("/storage/usage-inventory"),
  saveUsageSchedule: (payload: UsageSchedulePayload) =>
    apiClient.put<UsageScheduleSummary>("/storage/usage-schedule", payload),
  videoAssetFailures: (limit = 50, offset = 0, signal?: AbortSignal) =>
    apiClient.get<VideoAssetFailuresResponse>(
      `/storage/video-assets/failures?limit=${limit}&offset=${offset}`,
      { signal },
    ),
  retryVideoAsset: (payload: VideoAssetRetryPayload) =>
    apiClient.post<VideoAssetRetryResponse>("/storage/video-assets/retry", payload),
};
