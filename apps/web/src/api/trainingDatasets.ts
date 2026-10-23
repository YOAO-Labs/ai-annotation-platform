import { apiClient } from "./client";
import type {
  TrainingDatasetFrozenMask,
  TrainingDatasetInstanceMask,
  TrainingDatasetCreate,
  TrainingDatasetOut,
  TrainingDatasetPage,
  TrainingDatasetPreflight,
  TrainingDatasetSelection,
  TrainingDatasetSummary,
  TrainingDatasetInsightOverview,
  TrainingDatasetInsightComparison,
  TrainingDatasetClassPage,
  TrainingDatasetSamplePage,
  TrainingDatasetSampleDetail,
  TrainingDatasetAnalyzedSampleOut,
  ProjectTaskDatasetPage,
  UnrecognizedTrainingDatasetVersionPage,
  ProjectTaskDatasetOut,
} from "./generated/types.gen";

export type {
  TrainingDatasetCreate,
  TrainingDatasetSelection,
  TrainingDatasetSampleDetail,
  TrainingDatasetInstanceMask,
  TrainingDatasetOut,
  TrainingDatasetPreflight,
  TrainingDatasetSummary,
  TrainingDatasetInsightOverview,
  TrainingDatasetInsightComparison,
  TrainingDatasetClassPage,
  TrainingDatasetSamplePage,
  TrainingDatasetAnalyzedSampleOut,
};

export type DatasetSampleChange =
  | "all"
  | "added"
  | "removed"
  | "labels_changed"
  | "split_changed"
  | "multiplicity_changed"
  | "task_ids_changed"
  | "reintroduced"
  | "unchanged"
  | "unknown"
  | "source_relation_changed"
  | "source_evidence_updated"
  | "source_unknown";
export type DatasetSampleFilters = {
  limit?: number;
  offset?: number;
  split?: "train" | "val";
  class_index?: number;
  kind?: "all" | "positive" | "negative" | "excluded" | "unavailable";
  baseline_id?: string;
  change?: DatasetSampleChange;
  content_sha256?: string;
};

export type DatasetVersionFilters = {
  task?: ProjectTaskDatasetOut["task"];
  task_dataset_id?: string;
  q?: string;
  status?: string;
  created_from?: string;
  created_to?: string;
};

const queryString = (values: Record<string, string | number | undefined>) => {
  const query = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== "") query.set(key, String(value));
  });
  return query.toString();
};

const base = (projectId: string) => `/projects/${projectId}/training-datasets`;

export const trainingDatasetsApi = {
  taskDatasets: (projectId: string, options?: RequestInit) =>
    apiClient.get<ProjectTaskDatasetPage>(`/projects/${projectId}/task-datasets`, options),
  unrecognizedVersions: (projectId: string, offset = 0, options?: RequestInit, limit = 20) =>
    apiClient.get<UnrecognizedTrainingDatasetVersionPage>(
      `/projects/${projectId}/task-datasets/unrecognized-versions?${queryString({ limit, offset })}`,
      options,
    ),
  list: (
    projectId: string,
    offset = 0,
    status?: "ready",
    options?: RequestInit,
    filters: DatasetVersionFilters = {},
    limit = 20,
  ) =>
    apiClient.get<TrainingDatasetPage>(
      `${base(projectId)}?${queryString({ limit, offset, status, ...filters })}`,
      options,
    ),
  get: (projectId: string, id: string, options?: RequestInit) =>
    apiClient.get<TrainingDatasetOut>(`${base(projectId)}/${id}`, options),
  getSummary: (projectId: string, id: string, options?: RequestInit) =>
    apiClient.get<TrainingDatasetSummary>(
      `${base(projectId)}/${id}?include_manifest=false`,
      options,
    ),
  insights: (projectId: string, id: string, options?: RequestInit) =>
    apiClient.get<TrainingDatasetInsightOverview>(`${base(projectId)}/${id}/insights`, options),
  classes: (
    projectId: string,
    id: string,
    values: { limit?: number; offset?: number; search?: string } = {},
    options?: RequestInit,
  ) =>
    apiClient.get<TrainingDatasetClassPage>(
      `${base(projectId)}/${id}/insights/classes?${queryString(values)}`,
      options,
    ),
  compare: (projectId: string, id: string, baselineId: string, options?: RequestInit) =>
    apiClient.get<TrainingDatasetInsightComparison>(
      `${base(projectId)}/${id}/insights/compare?${queryString({ baseline_id: baselineId })}`,
      options,
    ),
  samples: (projectId: string, id: string, filters: DatasetSampleFilters, options?: RequestInit) =>
    apiClient.get<TrainingDatasetSamplePage>(
      `${base(projectId)}/${id}/insights/samples?${queryString(filters)}`,
      options,
    ),
  sample: (
    projectId: string,
    id: string,
    taskId: string,
    offset = 0,
    options?: RequestInit,
    limit = 500,
  ) =>
    apiClient.get<TrainingDatasetSampleDetail>(
      `${base(projectId)}/${id}/insights/samples/${taskId}?limit=${limit}&offset=${offset}`,
      options,
    ),
  sampleMask: (
    projectId: string,
    id: string,
    taskId: string,
    annotationId: string,
    options?: RequestInit,
  ) =>
    apiClient.get<TrainingDatasetFrozenMask>(
      `${base(projectId)}/${id}/insights/samples/${taskId}/masks/${annotationId}`,
      options,
    ),
  sampleMedia: (projectId: string, id: string, taskId: string, options?: RequestInit) =>
    apiClient.getBlob(
      `${base(projectId)}/${id}/insights/samples/${taskId}/media?preview=true`,
      options,
    ),
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
