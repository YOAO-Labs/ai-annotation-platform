import { apiClient } from "./client";
import type { StorageMaintenancePreview, StorageMaintenanceStatus } from "./generated/types.gen";

export type StorageMaintenanceOperation = StorageMaintenancePreview["operation"];

export const storageMaintenanceApi = {
  status: (id: string, signal?: AbortSignal) =>
    apiClient.get<StorageMaintenanceStatus>(`/storage-locations/${id}/maintenance`, { signal }),
  preview: (id: string, operation: StorageMaintenanceOperation, revision: number) =>
    apiClient.post<StorageMaintenancePreview>(`/storage-locations/${id}/maintenance-preview`, {
      operation,
      expected_revision: revision,
    }),
  apply: (id: string, review: StorageMaintenancePreview) =>
    apiClient.post<StorageMaintenanceStatus>(`/storage-locations/${id}/maintenance`, {
      operation: review.operation,
      expected_revision: review.revision,
      impact_token: review.impact_token,
    }),
};
