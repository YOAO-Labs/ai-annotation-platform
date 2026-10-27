import { apiClient } from "./client";
import type { ManagedOriginalReclamationPreview } from "./generated/types.gen";

export const storageReclamationApi = {
  preview: (limit: number, offset: number, signal?: AbortSignal) =>
    apiClient.get<ManagedOriginalReclamationPreview>(
      `/storage/managed-original-reclamation?limit=${limit}&offset=${offset}`,
      { signal },
    ),
};
