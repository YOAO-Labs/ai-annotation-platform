import { apiClient } from "./client";
import type { TrainingStudyCreate } from "./generated/types.gen";

export type { TrainingStudyCreate };
export type StudyTrialPlan = {
  id: string;
  ordinal: number;
  values: Record<string, unknown>;
  config_digest: string;
  config: Record<string, unknown>;
};
export type StudyPlan = {
  study_id: string;
  policy_digest: string;
  objective: string;
  direction: "maximize";
  planned_epochs: number;
  budgets: TrainingStudyCreate["budgets"];
  trials: StudyTrialPlan[];
};
export type StudySummary = {
  id: string;
  name: string;
  status: string;
  scheduling: boolean;
  message: string | null;
  trial_count: number;
};
export type StudyTrial = {
  id: string;
  ordinal: number;
  values: Record<string, unknown>;
  config_digest: string;
  planned_epochs: number;
  run_id: string | null;
  status: string;
  actual_epochs: number | null;
  completion: "unknown" | "target_reached" | "early_stopped" | null;
  queue_seconds: number | null;
  lease_seconds: number | null;
  native_seconds: number;
  native_time_unknown: boolean;
  result_error: string | null;
  result: {
    value: number;
    compatibility_digest: string;
    attempt_id: string;
    report_sha256: string;
    weights_sha256: string;
  } | null;
};
export type TrainingStudy = Omit<StudySummary, "trial_count"> & {
  project_id: string;
  plan_digest: string;
  plan: StudyPlan;
  accounting: {
    consumed_seconds: number;
    reserved_seconds: number;
    remaining_seconds: number;
    overrun_seconds: number;
    unknown_attempt_ids: string[];
  };
  trials: StudyTrial[];
  rankings: { compatibility_digest: string; trial_ids: string[] }[];
};

const base = (projectId: string) => `/projects/${projectId}/training-studies`;
export const trainingStudiesApi = {
  preview: (projectId: string, body: TrainingStudyCreate, options?: RequestInit) =>
    apiClient.post<{ plan: StudyPlan; preview_digest: string }>(
      `${base(projectId)}:preview`,
      body,
      options,
    ),
  create: (projectId: string, body: TrainingStudyCreate, options?: RequestInit) =>
    apiClient.post<TrainingStudy>(base(projectId), body, options),
  list: (projectId: string, offset = 0, options?: RequestInit, limit = 20) =>
    apiClient.get<{ total: number; items: StudySummary[] }>(
      `${base(projectId)}?limit=${limit}&offset=${offset}`,
      options,
    ),
  get: (projectId: string, studyId: string, options?: RequestInit) =>
    apiClient.get<TrainingStudy>(`${base(projectId)}/${studyId}`, options),
  cancel: (projectId: string, studyId: string, options?: RequestInit) =>
    apiClient.post<TrainingStudy>(`${base(projectId)}/${studyId}/cancel`, undefined, options),
};
