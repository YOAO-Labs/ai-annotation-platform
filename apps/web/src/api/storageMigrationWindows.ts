import { apiClient } from "./client";

export interface MigrationWindowFinalization {
  finalization_id: string;
  revision: number;
  state: "bound" | "sealing" | "sealed" | "invalidating" | "invalidated";
  window_revision: number;
  created_at: string;
  updated_at: string;
  native_observed_at: string | null;
  sealed_at: string | null;
  invalidated_at: string | null;
  inventory_job_id: string | null;
  copy_id: string | null;
  counts: Record<string, number> | null;
  error_code: string | null;
  migration_ready: false;
}

export interface MigrationWindowItem {
  window_id: string;
  parent_copy_id: string;
  target_location_id: string;
  state: "draining" | "stopped" | "closing" | "closed";
  revision: number;
  source_gate_revision: number;
  source_config_revision: number;
  created_at: string;
  updated_at: string;
  created_by: string | null;
  registered_operations: number;
  registered_access_drained: boolean;
  source_quiescence_proven: false;
  migration_ready: false;
  finalization?: MigrationWindowFinalization | null;
}
export interface MigrationWindowHistory {
  items: MigrationWindowItem[];
  total: number;
  limit: number;
  offset: number;
}
const PATH = "/storage/migration-windows";
export const storageMigrationWindowsApi = {
  list: (offset: number, limit = 20, signal?: AbortSignal) =>
    apiClient.get<MigrationWindowHistory>(`${PATH}?limit=${limit}&offset=${offset}`, { signal }),
  get: (id: string, signal?: AbortSignal) =>
    apiClient.get<MigrationWindowItem>(`${PATH}/${encodeURIComponent(id)}`, { signal }),
  create: (parentCopyId: string) =>
    apiClient.post<MigrationWindowItem>(PATH, { parent_copy_id: parentCopyId }),
  transition: (id: string, action: "stop" | "close", expectedRevision: number) =>
    apiClient.post<MigrationWindowItem>(`${PATH}/${encodeURIComponent(id)}/transitions`, {
      action,
      expected_revision: expectedRevision,
    }),
};
