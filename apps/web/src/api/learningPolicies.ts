import { apiClient } from "./client";
import type {
  LearningPolicyOut,
  LearningPolicySave,
  LearningTriggerOut,
} from "./generated/types.gen";
export type { LearningPolicyOut, LearningPolicySave };
export type LearningTrigger = LearningTriggerOut & { run_id: string | null };
const base = (id: string) => `/projects/${id}/learning-policy`;
export const learningPoliciesApi = {
  get: (id: string) => apiClient.get<LearningPolicyOut | null>(base(id)),
  save: (id: string, body: LearningPolicySave) => apiClient.put<LearningPolicyOut>(base(id), body),
  disable: (id: string, revision: number) =>
    apiClient.post<LearningPolicyOut>(`${base(id)}/disable?expected_revision=${revision}`),
  check: (id: string) => apiClient.post<LearningPolicyOut>(`${base(id)}/check`),
  triggers: (id: string, offset = 0) =>
    apiClient.get<{ total: number; items: LearningTrigger[] }>(
      `${base(id)}/triggers?limit=20&offset=${offset}`,
    ),
  remove: (id: string) => apiClient.delete<void>(base(id)),
};
