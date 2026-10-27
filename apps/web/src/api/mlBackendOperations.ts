import { apiClient } from "./client";

export type ManagedMLProvider = "gsam2" | "sam3" | "yolo" | "onnxtools" | "rapidocr";
export type MLBackendOperationProvider = ManagedMLProvider;

export interface ManagedMLAuthItem {
  provider: ManagedMLProvider;
  service_name: string;
  url: string;
  credential_state: "missing" | "ready" | "invalid";
  registries: { id: string; name: string; auth_method: string; state: string }[];
  outcome?: "synced" | "initialized" | "unavailable" | "manual_preserved" | "error" | null;
  message?: string | null;
}

export interface ManagedMLAuthStatus {
  items: ManagedMLAuthItem[];
}

export interface MLBackendOperationJob {
  id: string;
  provider: MLBackendOperationProvider;
  status: "queued" | "running" | "succeeded" | "failed" | "interrupted";
  phase: "queued" | "validating" | "building" | "stopping" | "recreating" | "checking" | "complete";
  created_at: string;
  updated_at: string;
  events: { at: string; message: string; kind: "info" | "success" | "error" }[];
  error: string | null;
  deployment: string;
}

export interface MLBackendOperationsStatus {
  available: boolean;
  reason: string | null;
  deployment: string | null;
  runner_id: string | null;
  services: {
    provider: MLBackendOperationProvider;
    can_rebuild: boolean;
    reason: string | null;
  }[];
  jobs: MLBackendOperationJob[];
}

export interface MLBackendRebuildTarget {
  provider: MLBackendOperationProvider;
  expected_deployment: string;
  expected_runner_id: string;
}

const authBase = "/admin/ml-integrations/managed-auth";
const operationsBase = "/admin/ml-backend-operations";

export const mlBackendOperationsApi = {
  managedAuth: (options?: RequestInit) =>
    apiClient.silentGet<ManagedMLAuthStatus>(authBase, options),
  initialize: (providers?: ManagedMLProvider[]) =>
    apiClient.silentPost<ManagedMLAuthStatus>(authBase + "/initialize", { providers }),
  status: (options?: RequestInit) =>
    apiClient.silentGet<MLBackendOperationsStatus>(operationsBase + "/status", options),
  rebuild: (target: MLBackendRebuildTarget) =>
    apiClient.silentPost<MLBackendOperationJob>(operationsBase + "/jobs", target),
  job: (id: string, options?: RequestInit) =>
    apiClient.silentGet<MLBackendOperationJob>(
      `${operationsBase}/jobs/${encodeURIComponent(id)}`,
      options,
    ),
};
