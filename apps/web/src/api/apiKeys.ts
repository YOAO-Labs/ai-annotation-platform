import { apiClient } from "./client";

export interface ApiKey {
  id: string;
  name: string;
  key_prefix: string;
  scopes: string[];
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface ApiKeyCreated extends ApiKey {
  plaintext: string;
}

export interface ApiKeyCreatePayload {
  name: string;
  scopes: string[];
  expires_in_days?: number | null;
}

export interface ApiKeyUpdatePayload {
  name?: string;
  scopes?: string[];
  expires_in_days?: number | null;
}

export const apiKeysApi = {
  list: () => apiClient.get<ApiKey[]>("/me/api-keys"),
  create: (payload: ApiKeyCreatePayload) => apiClient.post<ApiKeyCreated>("/me/api-keys", payload),
  update: (id: string, payload: ApiKeyUpdatePayload) =>
    apiClient.patch<ApiKey>(`/me/api-keys/${id}`, payload),
  rotate: (id: string) => apiClient.post<ApiKeyCreated>(`/me/api-keys/${id}/rotate`, {}),
  revoke: (id: string) => apiClient.delete<void>(`/me/api-keys/${id}`),
};
