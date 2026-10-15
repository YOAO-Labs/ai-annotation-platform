import { apiClient } from "./client";
import type {
  TrainingDatasetCreate,
  TrainingDatasetOut,
  TrainingDatasetPage,
  TrainingDatasetPreflight,
  TrainingDatasetSelection,
  TrainingDatasetSummary,
} from "./generated/types.gen";

export type {
  TrainingDatasetOut,
  TrainingDatasetPreflight,
  TrainingDatasetSelection,
  TrainingDatasetSummary,
};

const base = (projectId: string) => `/projects/${projectId}/training-datasets`;

export const trainingDatasetsApi = {
  list: (projectId: string, offset = 0, status?: "ready") =>
    apiClient.get<TrainingDatasetPage>(
      `${base(projectId)}?limit=20&offset=${offset}${status ? `&status=${status}` : ""}`,
    ),
  get: (projectId: string, id: string) =>
    apiClient.get<TrainingDatasetOut>(`${base(projectId)}/${id}`),
  preflight: (projectId: string, body: TrainingDatasetSelection) =>
    apiClient.post<TrainingDatasetPreflight>(`${base(projectId)}:preflight`, body),
  create: (projectId: string, body: TrainingDatasetCreate) =>
    apiClient.post<TrainingDatasetOut>(base(projectId), body),
  retry: (projectId: string, id: string) =>
    apiClient.post<TrainingDatasetOut>(`${base(projectId)}/${id}/retry`),
  download: (projectId: string, id: string) =>
    apiClient.get<{ url: string; sha256: string }>(`${base(projectId)}/${id}/download`),
  remove: (projectId: string, id: string) => apiClient.delete<void>(`${base(projectId)}/${id}`),
};
