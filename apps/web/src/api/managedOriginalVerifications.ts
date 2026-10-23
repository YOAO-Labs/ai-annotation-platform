import { apiClient } from "./client";
import type {
  ManagedOriginalVerificationHistory,
  ManagedOriginalVerificationStart,
} from "./generated/types.gen";

const path = (id: string) => `/storage/managed-original-retirements/${id}/verifications`;

export const managedOriginalVerificationsApi = {
  history: (id: string, limit: number, offset: number, signal?: AbortSignal) =>
    apiClient.get<ManagedOriginalVerificationHistory>(
      `${path(id)}?limit=${limit}&offset=${offset}`,
      {
        signal,
      },
    ),
  start: (id: string) => apiClient.post<ManagedOriginalVerificationStart>(path(id)),
};
