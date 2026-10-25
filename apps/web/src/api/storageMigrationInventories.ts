import { apiClient } from "./client";
import type { AsyncJobStatus } from "./asyncJobs";

export interface StorageMigrationInventoryLimits {
  max_pages: number;
  max_entries: number;
  max_object_bytes: number;
  max_total_bytes: number;
  max_seconds: number;
  read_timeout_seconds: number;
  page_size: number;
}

export interface StorageMigrationInventoryCounts {
  buckets: number;
  objects: number;
  delete_markers: number;
  multipart: number;
  verified_objects: number;
  verified_bytes: number;
  bytes_read: number;
  pages: number;
  issues: number;
  entries: number;
}

export interface StorageMigrationInventoryItem {
  job_id: string;
  status: AsyncJobStatus;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  scope: "selected-configured-source";
  source_location_id: string;
  migration_ready: false;
  limits: StorageMigrationInventoryLimits;
  handling: Record<string, "migrate" | "rebuild" | "retain" | "unreviewed">;
  cancel_requested: boolean;
  complete: boolean | null;
  counts: StorageMigrationInventoryCounts | null;
  error_code: string | null;
  report_available: boolean;
  report_sha256: string | null;
  report_size_bytes: number | null;
}

export interface StorageMigrationInventoryHistory {
  items: StorageMigrationInventoryItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface StorageMigrationInventoryStart {
  item: StorageMigrationInventoryItem;
  reused: boolean;
}

const PATH = "/storage/migration-inventories";

export const storageMigrationInventoriesApi = {
  list: (offset: number, limit = 20, signal?: AbortSignal) =>
    apiClient.get<StorageMigrationInventoryHistory>(`${PATH}?limit=${limit}&offset=${offset}`, {
      signal,
    }),
  start: (limits: StorageMigrationInventoryLimits) =>
    apiClient.post<StorageMigrationInventoryStart>(PATH, { limits }),
  dispatch: (jobId: string) =>
    apiClient.post<StorageMigrationInventoryStart>(`${PATH}/${encodeURIComponent(jobId)}/dispatch`),
  report: (jobId: string, signal?: AbortSignal) =>
    apiClient.silentGetBlob(`${PATH}/${encodeURIComponent(jobId)}/report`, { signal }),
};
