import { apiClient } from "./client";
import type {
  TrainingAttemptOut,
  TrainingParameters,
  TrainingRunCreate,
  TrainingRunOut,
  TrainingParameterSchema,
  TrainingTemplate,
  TrainingEvaluationOut,
} from "./generated/types.gen";

export type {
  TrainingAttemptOut,
  TrainingParameters,
  TrainingRunCreate,
  TrainingRunOut,
  TrainingEvaluationOut,
};

export interface TrainingCatalog {
  enabled: boolean;
  online: boolean;
  busy: boolean;
  resource_id: string | null;
  items: TrainingTemplate[];
}

export interface TrainingEpochEvent {
  sequence: number;
  kind: string;
  epoch: number | null;
  metrics: Record<string, number>;
}

const base = (projectId: string) => `/projects/${projectId}/training-runs`;
export const trainingRunsApi = {
  parameterSchema: (
    projectId: string,
    framework: string,
    baseModel: string,
    options?: RequestInit,
  ) =>
    apiClient.get<TrainingParameterSchema>(
      `${base(projectId)}/parameter-schema?framework=${encodeURIComponent(framework)}&base_model=${encodeURIComponent(baseModel)}`,
      options,
    ),
  catalog: (projectId: string, options?: RequestInit) =>
    apiClient.get<TrainingCatalog>(`${base(projectId)}/catalog`, options),
  list: (projectId: string, offset = 0, options?: RequestInit) =>
    apiClient.get<{ total: number; items: TrainingRunOut[] }>(
      `${base(projectId)}?limit=20&offset=${offset}`,
      options,
    ),
  get: (projectId: string, id: string, options?: RequestInit) =>
    apiClient.get<{ run: TrainingRunOut; attempts: TrainingAttemptOut[] }>(
      `${base(projectId)}/${id}`,
      options,
    ),
  events: (projectId: string, id: string, attemptId?: string, after = 0, options?: RequestInit) =>
    apiClient.get<{ attempt_id: string; items: TrainingEpochEvent[] }>(
      `${base(projectId)}/${id}/events?after=${after}${attemptId ? `&attempt_id=${encodeURIComponent(attemptId)}` : ""}`,
      options,
    ),
  evaluation: (
    projectId: string,
    id: string,
    attemptId: string,
    offset = 0,
    options?: RequestInit,
  ) =>
    apiClient.get<TrainingEvaluationOut>(
      `${base(projectId)}/${id}/attempts/${attemptId}/evaluation?limit=50&offset=${offset}`,
      options,
    ),
  preflight: (projectId: string, body: TrainingRunCreate, options?: RequestInit) =>
    apiClient.post<{ config: Record<string, unknown>; config_digest: string }>(
      `${base(projectId)}:preflight`,
      body,
      options,
    ),
  create: (projectId: string, body: TrainingRunCreate) =>
    apiClient.post<TrainingRunOut>(base(projectId), body),
  cancel: (projectId: string, id: string) =>
    apiClient.post<TrainingRunOut>(`${base(projectId)}/${id}/cancel`),
  resume: (projectId: string, id: string) =>
    apiClient.post<TrainingRunOut>(`${base(projectId)}/${id}/resume`),
  remove: (projectId: string, id: string) => apiClient.delete<void>(`${base(projectId)}/${id}`),
  download: (projectId: string, id: string, attemptId: string, name: string) =>
    apiClient.get<{ url: string; sha256: string; size_bytes: number }>(
      `${base(projectId)}/${id}/attempts/${attemptId}/artifacts/${encodeURIComponent(name)}/download`,
    ),
};
