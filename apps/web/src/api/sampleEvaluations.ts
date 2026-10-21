import { apiClient } from "./client";
import type {
  EvaluationAggregate as DetectionEvaluationAggregate,
  MaskEvaluationAggregate,
  EvaluationProfile,
  EvaluationReport,
  EvaluationSamplePage,
  EvaluationSourceOut,
  SampleEvaluationCreate,
  SampleEvaluationOut,
} from "./generated/types.gen";

export type {
  EvaluationReport,
  EvaluationSampleResult,
  EvaluationSourceOut,
  SampleEvaluationCreate,
  SampleEvaluationOut,
  MaskPrediction,
  MaskTruth,
  MaskEvaluationAggregate,
} from "./generated/types.gen";

export type EvaluationAggregate = DetectionEvaluationAggregate | MaskEvaluationAggregate;

export type EvaluationFilters = {
  class_index?: number;
  error?: "false_positive" | "false_negative" | "confusion" | "localization" | "failed";
  confidence_min?: number;
  confidence_max?: number;
  offset?: number;
  limit?: number;
};
export type EvaluationQualification = {
  model_ref: Record<string, unknown>;
  profile: EvaluationProfile;
  evaluator: Record<string, unknown>;
  preprocessing: Record<string, unknown>;
  limits: Record<string, number>;
};
export type EvaluationComparison = {
  compatible: boolean;
  reasons: string[];
  candidates: Array<{
    evaluation_id: string;
    attempt_id: string;
    name: string;
    status: SampleEvaluationOut["status"];
    model_ref: Record<string, unknown>;
    spec_digest: string;
    comparison_digest: string;
    aggregate: EvaluationAggregate | null;
  }>;
  delta_right_minus_left:
    | (Record<"tp" | "fp" | "fn" | "precision" | "recall" | "f1", number | null> &
        Partial<Record<"matched_mean_iou" | "matched_mean_dice", number | null>>)
    | null;
  sample_pairs: Array<{ left_ordinal: number; right_ordinal: number }>;
};

const base = (projectId: string) => `/projects/${projectId}/sample-evaluations`;
const attemptBase = (projectId: string, id: string, attemptId: string) =>
  `${base(projectId)}/${id}/attempts/${attemptId}`;
const query = (values: Record<string, string | number | undefined>) => {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined) params.set(key, String(value));
  });
  return params.toString();
};

export const sampleEvaluationsApi = {
  list: (projectId: string, offset = 0, options?: RequestInit, limit = 20) =>
    apiClient.get<{ items: SampleEvaluationOut[]; total: number }>(
      `${base(projectId)}?limit=${limit}&offset=${offset}`,
      options,
    ),
  get: (projectId: string, id: string, options?: RequestInit) =>
    apiClient.get<SampleEvaluationOut>(`${base(projectId)}/${id}`, options),
  qualify: (projectId: string, modelId: string, deploymentId: string, options?: RequestInit) =>
    apiClient.get<EvaluationQualification>(
      `${base(projectId)}/qualification?${query({ model_version_id: modelId, deployment_id: deploymentId })}`,
      options,
    ),
  create: (projectId: string, body: SampleEvaluationCreate, options?: RequestInit) =>
    apiClient.post<SampleEvaluationOut>(base(projectId), body, options),
  cancel: (projectId: string, id: string, options?: RequestInit) =>
    apiClient.post<SampleEvaluationOut>(`${base(projectId)}/${id}/cancel`, undefined, options),
  retry: (projectId: string, id: string, options?: RequestInit) =>
    apiClient.post<SampleEvaluationOut>(`${base(projectId)}/${id}/retry`, undefined, options),
  remove: (projectId: string, id: string) => apiClient.delete<void>(`${base(projectId)}/${id}`),
  report: (projectId: string, id: string, attemptId: string, options?: RequestInit) =>
    apiClient.get<EvaluationReport>(`${attemptBase(projectId, id, attemptId)}/report`, options),
  samples: (
    projectId: string,
    id: string,
    attemptId: string,
    filters: EvaluationFilters,
    options?: RequestInit,
  ) =>
    apiClient.get<EvaluationSamplePage>(
      `${attemptBase(projectId, id, attemptId)}/samples?${query(filters)}`,
      options,
    ),
  image: (
    projectId: string,
    id: string,
    attemptId: string,
    ordinal: number,
    options?: RequestInit,
  ) =>
    apiClient.getBlob(`${attemptBase(projectId, id, attemptId)}/samples/${ordinal}/image`, options),
  source: (
    projectId: string,
    id: string,
    attemptId: string,
    ordinal: number,
    options?: RequestInit,
  ) =>
    apiClient.get<EvaluationSourceOut>(
      `${attemptBase(projectId, id, attemptId)}/samples/${ordinal}/source`,
      options,
    ),
  compare: (projectId: string, leftId: string, rightId: string, options?: RequestInit) =>
    apiClient.get<EvaluationComparison>(
      `${base(projectId)}/compare?${query({ left_id: leftId, right_id: rightId })}`,
      options,
    ),
};
