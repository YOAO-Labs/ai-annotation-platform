import { apiClient } from "./client";

export type StorageConnectionKind = "s3" | "sftp";
export type StorageConnectionScope = "global" | "owner";

export interface StorageConnection {
  id: string;
  revision: number;
  name: string;
  kind: StorageConnectionKind;
  config: Record<string, unknown>;
  scope: StorageConnectionScope;
  project_id: string | null;
  secret_set: boolean;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface StorageConnectionCreatePayload {
  name: string;
  kind: StorageConnectionKind;
  config: Record<string, unknown>;
  secret: Record<string, unknown>;
  scope?: StorageConnectionScope;
}

export interface StorageConnectionUpdatePayload {
  expected_revision: number;
  name?: string;
  config?: Record<string, unknown>;
  secret?: Record<string, unknown>;
}

export interface StorageConnectionTestResult {
  ok: boolean;
  message: string;
  sample_count: number | null;
}

export interface ConnectorAllowlist {
  entries: string[];
  source: "database" | "environment";
}

export interface DeploymentSftpPreset {
  enabled: boolean;
  host: string | null;
  port: number;
}

export interface DatasetImportFromConnectionPayload {
  connection_id: string;
  source_path?: string;
  recursive?: boolean;
  include_globs?: string[];
}

export interface DatasetImportFromConnectionResponse {
  job_id: string;
}

export const storageConnectionsApi = {
  getAllowlist: (signal?: AbortSignal) =>
    apiClient.get<ConnectorAllowlist>("/storage-connections/allowlist", { signal }),

  updateAllowlist: (entries: string[]) =>
    apiClient.put<ConnectorAllowlist>("/storage-connections/allowlist", { entries }),

  resetAllowlist: () => apiClient.delete<ConnectorAllowlist>("/storage-connections/allowlist"),

  getDeploymentSftpPreset: (signal?: AbortSignal) =>
    apiClient.get<DeploymentSftpPreset>("/storage-connections/deployment-sftp-preset", { signal }),

  list: (signal?: AbortSignal) =>
    apiClient.get<StorageConnection[]>("/storage-connections", { signal }),

  create: (payload: StorageConnectionCreatePayload) =>
    apiClient.post<StorageConnection>("/storage-connections", payload),

  get: (id: string, signal?: AbortSignal) =>
    apiClient.get<StorageConnection>(`/storage-connections/${id}`, { signal }),

  update: (id: string, payload: StorageConnectionUpdatePayload) =>
    apiClient.patch<StorageConnection>(`/storage-connections/${id}`, payload),

  delete: (id: string) => apiClient.delete<void>(`/storage-connections/${id}`),

  test: (id: string) =>
    apiClient.post<StorageConnectionTestResult>(`/storage-connections/${id}/test`),

  importFromConnection: (datasetId: string, payload: DatasetImportFromConnectionPayload) =>
    apiClient.post<DatasetImportFromConnectionResponse>(
      `/datasets/${datasetId}/import-from-connection`,
      payload,
    ),
};
