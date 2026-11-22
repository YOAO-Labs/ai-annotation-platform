import { apiClient } from "./client";
import type { CommentCanvasDrawing, CommentMention } from "./comments";
import type { DiscussionActions } from "@/pages/Workbench/state/discussionTypes";

export type FeedbackKind = "issue" | "comment" | "reject" | "bug";
export type FeedbackAnchorType = "project" | "task" | "annotation" | "pixel" | "point_cloud";
export type FeedbackStatus = "open" | "resolved" | "wont_fix";
export type FeedbackSeverity = "info" | "warn" | "blocker";

export interface MaskFeedbackCompareLocator {
  baseline_kind: "previous_version" | "tracker_candidate" | "ai_candidate" | "neighbor_keyframe";
  mode: "overlay" | "boundary" | "xor" | "added" | "removed";
  current_digest: string;
  baseline_digest: string;
  candidate_job_id?: string | null;
  candidate_job_revision?: number | null;
  candidate_digest?: string | null;
  candidate_instance_id?: string | null;
}

export interface FeedbackVideoViewport {
  center_x: number;
  center_y: number;
  zoom: number;
}

export interface FeedbackVideoFrameRange {
  from_frame: number;
  to_frame: number;
}

export interface FeedbackVideoTimelineWindow {
  from: number;
  to: number;
}

export interface FeedbackVideoContext {
  schema_version: 1;
  track_id?: string | null;
  annotation_version?: number | null;
  frame_range?: FeedbackVideoFrameRange | null;
  viewport?: FeedbackVideoViewport | null;
  timeline_window?: FeedbackVideoTimelineWindow | null;
}

export interface FeedbackAnchorPosition {
  x?: number | null;
  y?: number | null;
  frame?: number | null;
  region_bbox?: [number, number, number, number] | null;
  region_digest?: string | null;
  boundary_digest?: string | null;
  mask_qc_issue_id?: string | null;
  point_cloud_quality_issue_id?: string | null;
  scene_id?: string | null;
  scene_track_id?: string | null;
  auxiliary_layers?: string[];
  compare_locator?: MaskFeedbackCompareLocator | null;
  video_context?: FeedbackVideoContext | Record<string, unknown> | null;
}

export interface AnnotationFeedback {
  id: string;
  kind: FeedbackKind;
  anchor_type: FeedbackAnchorType;
  project_id: string;
  task_id: string | null;
  annotation_id: string | null;
  anchor_position: FeedbackAnchorPosition | null;
  status: FeedbackStatus;
  severity: FeedbackSeverity | null;
  title: string | null;
  body: string;
  author_id: string;
  author_name: string | null;
  attachments: Array<Record<string, unknown>>;
  mentions?: CommentMention[];
  canvas_drawing?: CommentCanvasDrawing | null;
  thread_parent_id: string | null;
  is_active: boolean;
  resolved_at: string | null;
  resolved_by_id: string | null;
  created_at: string;
  updated_at: string | null;
  actions?: DiscussionActions;
}

export type PixelAnchoredFeedback = AnnotationFeedback & {
  anchor_type: "pixel";
  anchor_position: FeedbackAnchorPosition & { x: number; y: number };
};

export function hasPixelAnchor(feedback: AnnotationFeedback): feedback is PixelAnchoredFeedback {
  return (
    feedback.anchor_type === "pixel" &&
    typeof feedback.anchor_position?.x === "number" &&
    typeof feedback.anchor_position.y === "number"
  );
}

export interface AnnotationFeedbackListPage {
  items: AnnotationFeedback[];
  next_cursor: string | null;
  total?: number | null;
  status_counts?: Partial<Record<FeedbackStatus, number>> | null;
}

export interface AnnotationFeedbackThreadPage {
  root: AnnotationFeedback;
  items: AnnotationFeedback[];
  next_cursor: string | null;
  total: number;
}

export interface CreateFeedbackPayload {
  kind: FeedbackKind;
  anchor_type: FeedbackAnchorType;
  project_id: string;
  task_id?: string | null;
  annotation_id?: string | null;
  anchor_position?: FeedbackAnchorPosition | null;
  severity?: FeedbackSeverity | null;
  title?: string | null;
  body: string;
  attachments?: Array<Record<string, unknown>>;
  mentions?: CommentMention[];
  canvas_drawing?: CommentCanvasDrawing | null;
  thread_parent_id?: string | null;
}

export interface PatchFeedbackPayload {
  status?: FeedbackStatus;
  severity?: FeedbackSeverity;
  title?: string;
  body?: string;
  mentions?: CommentMention[];
}

export interface ListFeedbacksParams {
  project_id: string;
  task_id?: string;
  annotation_id?: string;
  kind?: FeedbackKind;
  anchor_type?: FeedbackAnchorType;
  status?: FeedbackStatus;
  limit?: number;
  cursor?: string;
  root_only?: boolean;
  include_counts?: boolean;
}

export interface ListFeedbackThreadParams {
  limit?: number;
  cursor?: string;
}

function buildQuery(params: ListFeedbacksParams | ListFeedbackThreadParams): string {
  const sp = new URLSearchParams();
  const entries = Object.entries(params) as Array<[string, unknown]>;
  for (const [k, v] of entries) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const qs = sp.toString();
  return qs ? `?${qs}` : "";
}

export const feedbacksApi = {
  list: (params: ListFeedbacksParams, signal?: AbortSignal) =>
    apiClient.get<AnnotationFeedbackListPage>(`/feedbacks${buildQuery(params)}`, { signal }),

  create: (payload: CreateFeedbackPayload) =>
    apiClient.post<AnnotationFeedback>("/feedbacks", payload),

  patch: (id: string, payload: PatchFeedbackPayload) =>
    apiClient.patch<AnnotationFeedback>(`/feedbacks/${id}`, payload),

  remove: (id: string) => apiClient.delete<void>(`/feedbacks/${id}`),

  reply: (id: string, payload: { body: string; attachments?: Array<Record<string, unknown>> }) =>
    apiClient.post<AnnotationFeedback>(`/feedbacks/${id}/replies`, payload),

  thread: (rootFeedbackId: string, params: ListFeedbackThreadParams = {}, signal?: AbortSignal) =>
    apiClient.get<AnnotationFeedbackThreadPage>(
      `/feedbacks/${rootFeedbackId}/thread${buildQuery(params)}`,
      { signal },
    ),
};
