import { apiClient } from "./client";
import type {
  TrainingAttemptOut,
  TrainingParameters,
  TrainingRunCreate,
  TrainingRunOut,
  TrainingParameterSchema,
} from "./generated/types.gen";

export type { TrainingAttemptOut, TrainingParameters, TrainingRunCreate, TrainingRunOut };

export interface TrainingCatalog {
  enabled: boolean;
  online: boolean;
  busy: boolean;
  resource_id: string | null;
  items: {
    framework: "ultralytics" | "libreyolo";
    version: string;
    image_digest: string;
    base_models: { id: string; sha256: string; filename: string }[];
    capabilities: {
      task_types: string[];
      resume: boolean;
      artifact_download: boolean;
      model_registration: boolean;
    };
  }[];
}

export interface TrainingEpochEvent {
  sequence: number;
  kind: string;
  epoch: number | null;
  metrics: Record<string, number>;
}

const base = (projectId: string) => `/projects/${projectId}/training-runs`;
export const trainingRunsApi = {
  parameterSchema: (projectId: string, framework: string, baseModel: string) =>
    apiClient.get<TrainingParameterSchema>(
      `${base(projectId)}/parameter-schema?framework=${encodeURIComponent(framework)}&base_model=${encodeURIComponent(baseModel)}`,
    ),
  catalog: (projectId: string) => apiClient.get<TrainingCatalog>(`${base(projectId)}/catalog`),
  list: (projectId: string, offset = 0) =>
    apiClient.get<{ total: number; items: TrainingRunOut[] }>(
      `${base(projectId)}?limit=20&offset=${offset}`,
    ),
  get: (projectId: string, id: string) =>
    apiClient.get<{ run: TrainingRunOut; attempts: TrainingAttemptOut[] }>(
      `${base(projectId)}/${id}`,
    ),
  events: (projectId: string, id: string, attemptId?: string, after = 0) =>
    apiClient.get<{ attempt_id: string; items: TrainingEpochEvent[] }>(
      `${base(projectId)}/${id}/events?after=${after}${attemptId ? `&attempt_id=${encodeURIComponent(attemptId)}` : ""}`,
    ),
  preflight: (projectId: string, body: TrainingRunCreate) =>
    apiClient.post<{ config: Record<string, unknown>; config_digest: string }>(
      `${base(projectId)}:preflight`,
      body,
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
