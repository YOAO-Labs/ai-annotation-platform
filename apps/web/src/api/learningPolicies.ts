import { apiClient } from "./client";
import type {
  LearningPolicyOut,
  LearningPolicySave,
  LearningTriggerOut,
} from "./generated/types.gen";
export type { LearningPolicyOut, LearningPolicySave };
export type LearningTask = NonNullable<LearningPolicySave["task"]>;
export type LearningTrigger = LearningTriggerOut & { run_id: string | null };
const base = (id: string) => `/projects/${id}/learning-policy`;
const taskQuery = (task: LearningTask, separator = "?") =>
  task === "detect" ? "" : `${separator}task=${task}`;
export const learningPoliciesApi = {
  get: (id: string, task: LearningTask = "detect") =>
    apiClient.get<LearningPolicyOut | null>(`${base(id)}${taskQuery(task)}`),
  save: (id: string, body: LearningPolicySave) => apiClient.put<LearningPolicyOut>(base(id), body),
  disable: (id: string, revision: number, task: LearningTask = "detect") =>
    apiClient.post<LearningPolicyOut>(
      `${base(id)}/disable?expected_revision=${revision}${taskQuery(task, "&")}`,
    ),
  check: (id: string, task: LearningTask = "detect") =>
    apiClient.post<LearningPolicyOut>(`${base(id)}/check${taskQuery(task)}`),
  triggers: (id: string, offset = 0, task: LearningTask = "detect", limit = 20) =>
    apiClient.get<{ total: number; items: LearningTrigger[] }>(
      `${base(id)}/triggers?limit=${limit}&offset=${offset}${taskQuery(task, "&")}`,
    ),
  remove: (id: string, task: LearningTask = "detect") =>
    apiClient.delete<void>(`${base(id)}${taskQuery(task)}`),
};
