import { apiClient } from "./client";
import type { CocoRleContent } from "./generated/types.gen";

export type SelectionTask = "detect" | "segment";
export type DetectionPrediction = { class_index: number; score: number; xyxy: number[] };
export type MaskPrediction = {
  index: number;
  class_index: number;
  native_class_index: number;
  score: number;
  rle: CocoRleContent;
  mask_sha256: string;
  area_pixels: number;
};

export type SelectionInput = {
  model_version_id: string;
  deployment_id: string;
  sample_task_ids: string[];
  seed: number;
  batch_size: number;
  max_inference_dispatches: number;
  max_review_tasks: number;
  confidence: number;
  nms_iou: number;
};
export type CreateRound = SelectionInput & {
  request_id: string;
  name: string;
  preview_digest: string;
};
export type Arm = "active" | "random_control";
export type Features = {
  low_confidence: number | null;
  ambiguity: number | null;
  priority: number | null;
  detections: number;
  possible_truncation: boolean;
  minimum_instance_pixels?: number | null;
  total_instance_pixels_including_overlap?: number;
  overlap_run_visits?: number;
};
export type FrozenSample = {
  ordinal: number;
  task_id: string;
  arm: Arm;
  sha256: string;
  width: number;
  height: number;
  group_task_ids: string[];
};
export type Preview = {
  preview_digest: string;
  pool_size: number;
  candidate_count: number;
  exclusion_reference_count: number;
  samples: FrozenSample[];
  omitted: Array<{ task_id: string; reason: string }>;
  model_ref: Record<string, unknown>;
  profile: Record<string, unknown>;
  strategy?: Record<string, unknown>;
  batch_size: number;
  max_inference_dispatches: number;
  max_review_tasks: number;
};
export type ActiveRound = {
  id: string;
  project_id: string;
  name: string;
  status: "queued" | "running" | "cancelling" | "lost" | "succeeded" | "failed" | "cancelled";
  spec_digest: string;
  spec: Omit<SelectionInput, "sample_task_ids" | "model_version_id" | "deployment_id"> & {
    round_id: string;
    samples: FrozenSample[];
    model_ref: Record<string, unknown>;
    pool_size: number;
    candidate_count: number;
    profile_digest: string;
    expires_at: number;
    task?: SelectionTask;
    schema?: number;
    strategy?: Record<string, unknown>;
  };
  inference_dispatches: number;
  accepted_tasks: number;
  current_attempt_id: string | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  attempts: Array<{
    id: string;
    ordinal: number;
    status: ActiveRound["status"];
    has_report: boolean;
    error_message: string | null;
  }>;
};
export type Ranking = {
  ordinal: number;
  task_id: string;
  arm: Arm;
  rank: number;
  recommended: boolean;
  reason: string;
  features: Features;
  evidence_digest: string;
};
export type RankingReport = {
  round_id: string;
  attempt_id: string;
  spec_digest: string;
  report_digest: string;
  ranking: Ranking[];
  missing_signal_count: number;
  inference_dispatches: number;
};
export type Candidate = {
  ordinal: number;
  task_id: string;
  status: string;
  disposition: "accepted" | "rejected" | null;
  decision_id: string | null;
  has_image: boolean;
  evidence: {
    features: Features;
    predictions: Array<DetectionPrediction | MaskPrediction>;
  } | null;
};
export type DecisionInput = {
  request_id: string;
  action: "accept" | "reject";
  ordinals: number[];
  batch_name: string;
};
export type Decision = {
  id: string;
  request_id: string;
  action: "accept" | "reject";
  accepted_batch_id: string | null;
  snapshot: {
    batch: { id: string; name: string; display_id: string } | null;
    tasks: Array<{ task_id: string; arm: Arm; ordinal: number }>;
  };
  snapshot_digest: string;
  created_at: string;
};
export type FeedbackInput = {
  request_id: string;
  evaluations: Array<{ evaluation_id: string; attempt_id: string }>;
};
export type Feedback = {
  id: string;
  created_at: string;
  snapshot_digest: string;
  snapshot: {
    observed_at: string;
    arms: Array<{
      arm: Arm;
      accepted_tasks: number;
      planned_tasks: number;
      reviewed_tasks: number;
      reviewer_rejection_count: number;
      correction_count: number | null;
      effort: {
        annotate: { recorded_ms: number | null; coverage: string };
        review: { recorded_ms: number | null; coverage: string };
      };
    }>;
    evaluations: {
      comparable: boolean;
      reasons: string[];
      delta_second_minus_first: Record<string, number | null> | null;
      references: Array<{ evaluation_id: string; attempt_id: string; name: string }>;
    };
    limitations: string[];
    efficiency_claim: false;
  };
};
const base = (projectId: string) => `/projects/${projectId}/active-learning-rounds`;
export const activeLearningApi = {
  pool: (projectId: string, offset = 0, options?: RequestInit) =>
    apiClient.get<{
      total: number;
      items: Array<{ task_id: string; display_id: string; file_name: string }>;
    }>(`${base(projectId)}/pool?limit=32&offset=${offset}`, options),
  preview: (projectId: string, body: SelectionInput) =>
    apiClient.post<Preview>(`${base(projectId)}:preview`, body),
  create: (projectId: string, body: CreateRound) =>
    apiClient.post<ActiveRound>(base(projectId), body),
  list: (projectId: string, offset = 0, options?: RequestInit, task?: SelectionTask, limit = 20) =>
    apiClient.get<{ items: ActiveRound[]; total: number }>(
      `${base(projectId)}?limit=${limit}&offset=${offset}${task ? `&task=${task}` : ""}`,
      options,
    ),
  get: (projectId: string, id: string, options?: RequestInit) =>
    apiClient.get<ActiveRound>(`${base(projectId)}/${id}`, options),
  report: (projectId: string, id: string, attemptId: string, options?: RequestInit) =>
    apiClient.get<RankingReport>(`${base(projectId)}/${id}/attempts/${attemptId}/report`, options),
  candidates: (projectId: string, id: string, options?: RequestInit) =>
    apiClient.get<{ items: Candidate[] }>(`${base(projectId)}/${id}/candidates`, options),
  image: (projectId: string, id: string, ordinal: number, options?: RequestInit) =>
    apiClient.getBlob(`${base(projectId)}/${id}/candidates/${ordinal}/image`, options),
  cancel: (projectId: string, id: string) =>
    apiClient.post<ActiveRound>(`${base(projectId)}/${id}/cancel`),
  retry: (projectId: string, id: string) =>
    apiClient.post<ActiveRound>(`${base(projectId)}/${id}/retry`),
  decisions: (projectId: string, id: string, options?: RequestInit, offset = 0, limit = 20) =>
    apiClient.get<{ items: Decision[]; total: number }>(
      `${base(projectId)}/${id}/decisions?limit=${limit}&offset=${offset}`,
      options,
    ),
  decide: (projectId: string, id: string, body: DecisionInput) =>
    apiClient.post<Decision>(`${base(projectId)}/${id}/decisions`, body),
  feedback: (projectId: string, id: string, options?: RequestInit, offset = 0, limit = 20) =>
    apiClient.get<{ items: Feedback[]; total: number }>(
      `${base(projectId)}/${id}/feedback?limit=${limit}&offset=${offset}`,
      options,
    ),
  recordFeedback: (projectId: string, id: string, body: FeedbackInput) =>
    apiClient.post<Feedback>(`${base(projectId)}/${id}/feedback`, body),
};
