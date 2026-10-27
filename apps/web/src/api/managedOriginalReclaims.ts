import { apiClient } from "./client";
import type {
  ManagedOriginalReclaimApply,
  ManagedOriginalReclaimHistory,
  ManagedOriginalReclaimPreview,
  ManagedOriginalReclaimReview,
  ManagedOriginalReclaimStart,
  ReferenceReconciliationOut,
} from "./generated/types.gen";

const path = (id: string) => `/storage/managed-original-retirements/${id}`;
const reconciliation = "/storage/managed-original-reference-reconciliation";

export const managedOriginalReclaimsApi = {
  history: (id: string, limit: number, offset: number, signal?: AbortSignal) =>
    apiClient.get<ManagedOriginalReclaimHistory>(
      `${path(id)}/reclaims?limit=${limit}&offset=${offset}`,
      { signal },
    ),
  preview: (id: string, payload: ManagedOriginalReclaimReview) =>
    apiClient.post<ManagedOriginalReclaimPreview>(`${path(id)}/reclaim-preview`, payload),
  start: (id: string, payload: ManagedOriginalReclaimApply) =>
    apiClient.post<ManagedOriginalReclaimStart>(`${path(id)}/reclaims`, payload),
  redispatch: (id: string) =>
    apiClient.post<ManagedOriginalReclaimStart>(
      `/storage/managed-original-reclaims/${id}/redispatch`,
    ),
  reconciliation: (signal?: AbortSignal) =>
    apiClient.get<ReferenceReconciliationOut>(reconciliation, { signal }),
  reconcile: () => apiClient.post<ReferenceReconciliationOut>(reconciliation),
};
