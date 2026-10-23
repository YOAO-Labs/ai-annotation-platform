import { apiClient } from "./client";
import type {
  DatasetQualityConfig,
  DatasetQualityRunOut,
  DatasetQualityRunList,
  DatasetQualityResultOut,
  DatasetQualityResultList,
  DatasetQualityReview,
} from "./generated/types.gen";
export type QualityConfig = DatasetQualityConfig;
export type QualityRun = DatasetQualityRunOut;
export type QualityResult = DatasetQualityResultOut;
export type QualityReview = DatasetQualityReview;
const base = (id: string) => `/datasets/${encodeURIComponent(id)}/quality-runs`;
export const datasetQualityApi = {
  list: (id: string, page: number, size: number, signal?: AbortSignal) =>
    apiClient.get<DatasetQualityRunList>(`${base(id)}?limit=${size}&offset=${page * size}`, {
      signal,
    }),
  get: (id: string, run: string, signal?: AbortSignal) =>
    apiClient.get<QualityRun>(`${base(id)}/${encodeURIComponent(run)}`, { signal }),
  results: (id: string, run: string, params: URLSearchParams, signal?: AbortSignal) =>
    apiClient.get<DatasetQualityResultList>(
      `${base(id)}/${encodeURIComponent(run)}/results?${params}`,
      { signal },
    ),
  start: (id: string, config: QualityConfig) => apiClient.post<QualityRun>(base(id), config),
  resume: (id: string, run: string) =>
    apiClient.post<QualityRun>(`${base(id)}/${encodeURIComponent(run)}/resume`, {}),
  review: (id: string, run: string, result: string, review: QualityReview) =>
    apiClient.post(
      `${base(id)}/${encodeURIComponent(run)}/results/${encodeURIComponent(result)}/review`,
      review,
    ),
};
