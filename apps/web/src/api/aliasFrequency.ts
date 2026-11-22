import { apiClient } from "./client";

export interface AliasFrequencyResponse {
  project_id: string;
  total_predictions: number;
  frequency: Record<string, number>;
  last_computed_at: string;
}

export const aliasFrequencyApi = {
  byProject: (projectId: string) =>
    apiClient.get<AliasFrequencyResponse>(`/admin/projects/${projectId}/alias-frequency`),
};
