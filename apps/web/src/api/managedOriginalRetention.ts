import { apiClient } from "./client";
import type {
  ManagedOriginalRetentionApply,
  ManagedOriginalRetentionChange,
  ManagedOriginalRetentionPreview,
  ManagedOriginalRetentionState,
} from "./generated/types.gen";

const path = (id: string) => `/storage/managed-original-retirements/${id}/retention`;

export const managedOriginalRetentionApi = {
  state: (id: string, limit: number, offset: number, signal?: AbortSignal) =>
    apiClient.get<ManagedOriginalRetentionState>(`${path(id)}?limit=${limit}&offset=${offset}`, {
      signal,
    }),
  preview: (id: string, payload: ManagedOriginalRetentionChange) =>
    apiClient.post<ManagedOriginalRetentionPreview>(`${path(id)}-preview`, payload),
  apply: (id: string, payload: ManagedOriginalRetentionApply) =>
    apiClient.post<ManagedOriginalRetentionState>(path(id), payload),
};
