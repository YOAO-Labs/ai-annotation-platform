import { apiClient } from "./client";
import type { AsyncJobStatus } from "./asyncJobs";

export const MIGRATION_PURPOSES = [
  ["annotations", "标注文件"],
  ["datasets", "托管数据集"],
  ["bug-reports", "问题附件"],
  ["media-cache", "派生媒体缓存"],
  ["audit-archive", "审计归档"],
  ["import", "导入暂存"],
  ["export", "导出文件"],
  ["avatars", "用户头像"],
  ["training", "冻结训练数据"],
] as const;
export type MigrationHandling = "migrate" | "rebuild" | "retain";
export type MigrationPurpose = (typeof MIGRATION_PURPOSES)[number][0];
export type MigrationCopyOperation = "prepare" | "run" | "verify";
export type MigrationCopyAttemptOperation = MigrationCopyOperation | "recover";
export interface MigrationCopyLimits {
  max_object_bytes: number;
  max_source_bytes: number;
  max_upload_bytes: number;
  max_verify_bytes: number;
  max_objects: number;
  max_seconds: number;
  connect_timeout_seconds: number;
  read_timeout_seconds: number;
}
export interface MigrationCopyCounts {
  verified: number;
  pending: number;
  writing: number;
  uncertain: number;
  failed: number;
  skipped: number;
}
export interface MigrationCopySourceSummary {
  complete: boolean;
  observed_versions_and_markers: number;
  objects: number;
  delete_markers: number;
  verified_objects: number;
  unverified_objects: number;
  over_object_limit: number;
  mutable_null_objects: number;
  unpinned_objects: number;
  exact_catalog_matches: number;
}
export interface MigrationCopyItem {
  copy_id: string;
  created_at: string;
  updated_at: string;
  source_inventory_job_id: string | null;
  source_report_sha256: string;
  source_complete: boolean;
  target_location_id: string;
  target_name: string | null;
  handling: Record<MigrationPurpose, MigrationHandling>;
  limits: MigrationCopyLimits;
  prepared: boolean;
  job_id: string | null;
  operation: MigrationCopyOperation | null;
  status: AsyncJobStatus | null;
  cancel_requested: boolean;
  counts: MigrationCopyCounts | null;
  decisions: Record<string, number> | null;
  source_summary: MigrationCopySourceSummary | null;
  catalog_summary: {
    complete: boolean;
    enumeration_complete: boolean;
    resolution_complete: boolean;
    counts: Record<string, number>;
  } | null;
  copy_subset_complete: boolean | null;
  migration_ready: false;
  error_code: string | null;
  attempt_subset_complete: boolean | null;
  selected_count: number | null;
  selected_verified_count: number | null;
  io: { source_bytes: number; upload_bytes: number; verify_bytes: number; objects: number } | null;
  checkpoint_available: boolean;
}
export interface MigrationCopyStart {
  item: MigrationCopyItem;
  reused: boolean;
}
export interface MigrationCopyTarget {
  location_id: string;
  name: string;
  config_revision: number;
}
export interface MigrationCopyFailedObject {
  object_id: string;
  size_bytes: number;
  error_code: string | null;
}
export interface MigrationCopyPage<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
export interface MigrationCopyCreate {
  inventory_job_id: string;
  target_location_id: string;
  handling: Record<MigrationPurpose, MigrationHandling>;
  limits: MigrationCopyLimits;
}
const PATH = "/storage/migration-copies";
export const storageMigrationCopiesApi = {
  targets: (inventoryJobId: string, offset: number, limit: number, signal?: AbortSignal) =>
    apiClient.get<MigrationCopyPage<MigrationCopyTarget>>(
      `/storage/migration-copy-targets?${new URLSearchParams({ inventory_job_id: inventoryJobId, limit: String(limit), offset: String(offset) })}`,
      { signal },
    ),
  create: (body: MigrationCopyCreate) => apiClient.post<MigrationCopyStart>(PATH, body),
  list: (offset: number, limit: number, signal?: AbortSignal) =>
    apiClient.get<MigrationCopyPage<MigrationCopyItem>>(`${PATH}?limit=${limit}&offset=${offset}`, {
      signal,
    }),
  get: (id: string, signal?: AbortSignal) =>
    apiClient.get<MigrationCopyItem>(`${PATH}/${encodeURIComponent(id)}`, { signal }),
  attempt: (
    id: string,
    operation: MigrationCopyAttemptOperation,
    retryFailed = false,
    retryObjectIds?: string[],
  ) =>
    apiClient.post<MigrationCopyStart>(`${PATH}/${encodeURIComponent(id)}/attempts`, {
      operation,
      ...(operation === "run"
        ? {
            retry_failed: retryFailed,
            ...(retryObjectIds ? { retry_object_ids: retryObjectIds } : {}),
          }
        : {}),
    }),
  failedObjects: (id: string, offset: number, limit: number, signal?: AbortSignal) =>
    apiClient.get<MigrationCopyPage<MigrationCopyFailedObject>>(
      `${PATH}/${encodeURIComponent(id)}/failed-objects?limit=${limit}&offset=${offset}`,
      { signal },
    ),
  dispatch: (id: string) =>
    apiClient.post<MigrationCopyStart>(`${PATH}/${encodeURIComponent(id)}/dispatch`),
  checkpoint: (id: string, signal?: AbortSignal) =>
    apiClient.silentGetBlob(`${PATH}/${encodeURIComponent(id)}/checkpoint`, { signal }),
};
